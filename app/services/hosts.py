from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.errors import ApiError, not_found
from ..config import get_settings
from ..ids import new_agent_rmq_password, new_agent_rmq_user, new_id
from ..messaging import (
    RabbitManagement,
    agent_permission_pattern,
    agent_write_pattern,
    declare_host_queues,
    delete_host_queues,
    get_channel,
)
from ..models import DEFAULT_HYPERVISOR, Cluster, Host

log = logging.getLogger(__name__)


async def provision_host_queues(host_ref: str) -> None:
    """host_ref is the host short_id."""
    channel = await get_channel()
    try:
        await declare_host_queues(channel, host_ref)
        log.info("provisioned queues for host=%s", host_ref)
    finally:
        await channel.close()


async def ensure_all_host_queues(db: AsyncSession) -> int:
    """Called on API startup: every known host must have its queues."""
    short_ids = (await db.execute(select(Host.short_id))).scalars().all()
    channel = await get_channel()
    try:
        for short_id in short_ids:
            await declare_host_queues(channel, short_id)
    finally:
        await channel.close()
    log.info("ensured queues for %d host(s)", len(short_ids))
    return len(short_ids)


def ensure_host_agent_credentials(host: Host) -> bool:
    """Fill in agent_rmq_user / agent_rmq_password on the row if missing.
    Returns True when the row was changed (caller must commit)."""
    changed = False
    if not host.agent_rmq_user:
        host.agent_rmq_user = new_agent_rmq_user(host.short_id)
        changed = True
    if not host.agent_rmq_password:
        host.agent_rmq_password = new_agent_rmq_password()
        changed = True
    return changed


async def ensure_host_agent_user(mgmt: RabbitManagement, host: Host) -> None:
    """Upsert the host agent's RabbitMQ user + scope its permissions to the
    host's own queues. Assumes credentials are already set on the row."""
    assert host.agent_rmq_user and host.agent_rmq_password
    own_queues = agent_permission_pattern(host.short_id)
    await mgmt.upsert_user(host.agent_rmq_user, host.agent_rmq_password)
    await mgmt.set_permissions(
        host.agent_rmq_user,
        configure=own_queues,
        # write also needs the default exchange the agent publishes through
        write=agent_write_pattern(host.short_id),
        read=own_queues,
    )
    log.info("ensured RabbitMQ agent user %s for host=%s", host.agent_rmq_user, host.id)


async def provision_host_agent_user(db: AsyncSession, host: Host) -> None:
    """One-host convenience used by create_host."""
    ensure_host_agent_credentials(host)
    await db.flush()
    async with RabbitManagement() as mgmt:
        await ensure_host_agent_user(mgmt, host)


def agent_amqp_url(host: Host) -> str:
    """The amqp:// URL this host's agent uses, with its own RabbitMQ credentials.

    Base (scheme/host/port/vhost) comes from OVC_AGENT_RABBITMQ_URL, falling back
    to OVC_RABBITMQ_URL. Credentials are always the per-host agent user/password.
    """
    settings = get_settings()
    parts = urlsplit(settings.agent_rabbitmq_url or settings.rabbitmq_url)
    scheme = parts.scheme or "amqp"
    netloc = parts.hostname or "localhost"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    vhost = parts.path or "/"
    user = quote(host.agent_rmq_user or "", safe="")
    password = quote(host.agent_rmq_password or "", safe="")
    return f"{scheme}://{user}:{password}@{netloc}{vhost}"


def render_agent_config(host: Host) -> str:
    """The `config.ini` an operator drops next to the agent binary on the host.
    `host_id` and `rabbitmq_url` are filled in; the storage paths are placeholders
    the operator must point at real directories before starting the service."""
    return (
        "[agent]\n"
        f"host_id = {host.short_id}\n"
        f"rabbitmq_url = {agent_amqp_url(host)}\n"
        "\n"
        "log_level = info\n"
        "task_timeout = 60\n"
        "refresh_interval_vms = 180\n"
        "refresh_interval_host = 600\n"
        "metrics_interval = 300\n"
        "max_concurrent_jobs = 4\n"
        "\n"
        "; Required - point these at real directories on this host:\n"
        "template_path = D:\\HyperV\\TEMPLATES\n"
        "local_iso_path = D:\\HyperV\\ISOS\n"
        "\n"
        "; Optional - extra VM storage roots, semicolon-separated:\n"
        "; aditional_vm_storage = E:\\HyperV\n"
    )


_INSTALL_DIR = "C:\\Program Files\\ovc-agent"


def _ps_here_string(body: str) -> str:
    """Embed ``body`` as a PowerShell literal (non-interpolating) here-string.
    The only terminator is ``'@`` at the start of a line, so a leading ``'@``
    inside the body is defused by prefixing a space (config.ini never has one)."""
    safe = "\n".join((" " + ln if ln.startswith("'@") else ln) for ln in body.splitlines())
    return f"@'\n{safe}\n'@"


def render_agent_install_script(
    host: Host, *, download_url: str, checksum_sha256: str, version: str, config_ini: str
) -> str:
    """A self-contained ``install.ps1`` served straight from the API. Run once on
    the Hyper-V host in an elevated PowerShell - it creates the install dir,
    downloads + checksums the agent binary, writes ``config.ini``, installs the
    Windows service, then opens the config in Notepad and tells the operator to
    fill in the storage paths and start the service themselves."""
    return f"""#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# ovc-agent installer for host '{host.name}' ({host.short_id})
# agent {version}  SHA-256 {checksum_sha256}

$InstallDir  = '{_INSTALL_DIR}'
$ExePath     = Join-Path $InstallDir 'ovc-agent.exe'
$ConfigPath  = Join-Path $InstallDir 'config.ini'
$ServiceName = 'ovc-agent'
$DownloadUrl = '{download_url}'
$Checksum    = '{checksum_sha256.upper()}'

Write-Host "Installing ovc-agent into $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne 'Stopped') {{
    Write-Host 'Stopping the running ovc-agent service so the binary can be replaced...'
    Stop-Service -Name $ServiceName -Force
}}

Write-Host 'Downloading the agent binary...'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$tmp = "$ExePath.download"
Invoke-WebRequest -Uri $DownloadUrl -OutFile $tmp -UseBasicParsing
$hash = (Get-FileHash $tmp -Algorithm SHA256).Hash
if ($hash -ne $Checksum) {{
    Remove-Item $tmp -Force
    throw "checksum mismatch: got $hash, expected $Checksum"
}}
Move-Item $tmp $ExePath -Force
Write-Host "Agent binary written to $ExePath"

$ConfigBody = {_ps_here_string(config_ini)}
if (Test-Path $ConfigPath) {{
    Write-Host "config.ini already exists at $ConfigPath - keeping it (not overwritten)."
}} else {{
    Set-Content -Path $ConfigPath -Value $ConfigBody -Encoding UTF8
    Write-Host "Wrote $ConfigPath"
}}

if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {{
    Write-Host 'Installing the ovc-agent Windows service...'
    & $ExePath install
    if ($LASTEXITCODE -ne 0) {{ throw "ovc-agent.exe install failed (exit $LASTEXITCODE)" }}
}} else {{
    Write-Host 'ovc-agent Windows service already installed.'
}}

try {{ Start-Process notepad.exe $ConfigPath }} catch {{
    Write-Host "Could not open Notepad - edit $ConfigPath by hand."
}}

Write-Host ''
Write-Host '============================================================'
Write-Host ' ovc-agent is installed but NOT started yet.'
Write-Host ''
Write-Host " 1. Edit  $ConfigPath  (opened in Notepad) and set:"
Write-Host '      template_path   = existing folder for exported VM templates'
Write-Host '      local_iso_path  = existing folder that holds your ISO files'
Write-Host '    then save the file.'
Write-Host ''
Write-Host ' 2. Start the service:'
Write-Host "      Start-Service $ServiceName"
Write-Host ''
Write-Host '    Confirm it is running:'
Write-Host "      Get-Service $ServiceName"
Write-Host ''
Write-Host '    Follow the agent log:'
Write-Host "      Get-Content '$InstallDir\\agent.log' -Wait -Tail 20"
Write-Host '============================================================'
"""


async def build_agent_install_script(db: AsyncSession, host: Host) -> tuple[str, str]:
    """Render this host's ``install.ps1``. Returns ``(script_text, filename)``.
    Raises ``ApiError`` when no active binary exists for the host's hypervisor,
    or when the store can't hand out a download URL (local mode needs
    ``OVC_PUBLIC_BASE_URL``)."""
    from .agent_store import get_agent_store
    from .agent_upgrade import active_agent_binary

    binary = await active_agent_binary(db, host.hypervisor)
    if binary is None:
        raise ApiError(
            "NO_ACTIVE_AGENT_BINARY",
            f"No active agent binary for hypervisor '{host.hypervisor}' - upload "
            "one in Agent Management first",
            status_code=409,
        )

    url = await get_agent_store().download_url(
        binary.id, binary.storage_key, binary.filename
    )
    if not url:
        raise ApiError(
            "AGENT_DOWNLOAD_UNAVAILABLE",
            "Local agent storage has no public download URL - set "
            "OVC_PUBLIC_BASE_URL so the Hyper-V host can reach this API",
            status_code=409,
        )

    script = render_agent_install_script(
        host,
        download_url=url,
        checksum_sha256=binary.checksum_sha256,
        version=binary.version,
        config_ini=render_agent_config(host),
    )
    return script, install_script_filename(host)


def install_script_filename(host: Host) -> str:
    return f"ovc-agent-install-{host.short_id}.ps1"


async def build_agent_install_url(
    db: AsyncSession, host: Host
) -> tuple[str, str, datetime]:
    """A tokenized ``install.ps1`` URL for this host plus an elevated-PowerShell
    one-liner that fetches and runs it. Returns ``(url, command, expires_at)``.
    Raises ``ApiError`` when there is no active binary or ``OVC_PUBLIC_BASE_URL``
    is unset (the Hyper-V host must be able to reach this API)."""
    from .agent_store import sign_scope
    from .agent_upgrade import active_agent_binary

    if await active_agent_binary(db, host.hypervisor) is None:
        raise ApiError(
            "NO_ACTIVE_AGENT_BINARY",
            f"No active agent binary for hypervisor '{host.hypervisor}' - upload "
            "one in Agent Management first",
            status_code=409,
        )

    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    if not base:
        raise ApiError(
            "AGENT_DOWNLOAD_UNAVAILABLE",
            "Set OVC_PUBLIC_BASE_URL (the URL a Hyper-V host uses to reach this "
            "API) to hand out an install-script URL",
            status_code=409,
        )

    ttl = settings.agent_install_url_ttl_seconds
    qs = urlencode(sign_scope(f"host-install:{host.id}", ttl))
    filename = install_script_filename(host)
    url = f"{base}{settings.api_prefix}/hosts/{host.id}/agent-install.ps1?{qs}"
    command = (
        f"iwr '{url}' -OutFile \"$env:TEMP\\{filename}\" -UseBasicParsing; "
        f'& "$env:TEMP\\{filename}"'
    )
    return url, command, datetime.now(UTC) + timedelta(seconds=ttl)


async def deprovision_host_messaging(
    host_ref: str, agent_rmq_user: str | None
) -> None:
    """Tear down a host's RabbitMQ queues and agent user on host deletion.
    `host_ref` is the host short_id.

    Best-effort: failures are logged, not raised. The DB row is the source of
    truth, and an orphaned queue or user on the broker is harmless - the startup
    preflight only ever *adds* what live hosts need, it never removes."""
    try:
        channel = await get_channel()
        try:
            await delete_host_queues(channel, host_ref)
        finally:
            await channel.close()
    except Exception:  # noqa: BLE001
        log.warning("could not delete queues for host=%s", host_ref, exc_info=True)

    if agent_rmq_user:
        try:
            async with RabbitManagement() as mgmt:
                await mgmt.delete_user(agent_rmq_user)
        except Exception:  # noqa: BLE001
            log.warning(
                "could not delete RabbitMQ user %s for host=%s",
                agent_rmq_user,
                host_ref,
                exc_info=True,
            )
    log.info("deprovisioned messaging for host=%s", host_ref)


async def create_host(
    db: AsyncSession,
    *,
    name: str,
    cluster_id: str | None = None,
    hypervisor: str = DEFAULT_HYPERVISOR,
) -> Host:
    hypervisor = str(hypervisor)
    cluster = await db.get(Cluster, cluster_id) if cluster_id is not None else None
    if cluster_id is not None and cluster is None:
        raise not_found("Cluster")
    if cluster is not None and cluster.hypervisor != hypervisor:
        raise ApiError(
            "INVALID",
            f"Cluster runs {cluster.hypervisor}; cannot add a {hypervisor} host",
        )
    host = Host(
        id=new_id(),
        name=name.strip(),
        cluster_id=cluster_id,
        hypervisor=hypervisor,
    )
    db.add(host)
    await db.flush()  # applies the short_id default
    await provision_host_queues(host.short_id)
    await provision_host_agent_user(db, host)
    return host
