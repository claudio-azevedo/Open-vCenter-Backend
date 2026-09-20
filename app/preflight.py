"""Startup preflight: verify - and where possible repair - everything the backend
needs before it serves traffic. Any check that can't be satisfied logs the reason
and raises PreflightError; the caller (API lifespan) then exits the process.

Checks, in order:
  1. Valkey    - connection + SET/GET round-trip
  2. Postgres  - connection
  3. schema    - `alembic upgrade head`, then confirm the DB is at head
  4. RabbitMQ  - AMQP connection + management API aliveness
  5. hosts     - every DB host has its queues, agent RabbitMQ user and permissions
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import inspect, select, text

from .cache import get_cache
from .config import get_settings
from .db import engine, session_scope
from .messaging import (
    ALL_KINDS,
    RabbitManagement,
    declare_host_queues,
    get_channel,
    get_connection,
    queue_name,
)
from .models import Host
from .services.hosts import ensure_host_agent_credentials, ensure_host_agent_user

log = logging.getLogger("ovc.preflight")

BASE_DIR = Path(__file__).resolve().parent.parent
_PROBE_KEY = "__ovc_preflight__"


class PreflightError(RuntimeError):
    """A startup check failed; the process should log and exit."""


# --- 1. Valkey --------------------------------------------------------------

async def _check_valkey() -> None:
    cache = get_cache()
    if not await cache.ping():
        raise PreflightError("Valkey did not answer PING")
    await cache.set(_PROBE_KEY, "1", ex=10)
    if await cache.get(_PROBE_KEY) != "1":
        raise PreflightError("Valkey SET/GET round-trip failed (read-only replica?)")
    await cache.delete(_PROBE_KEY)
    log.info("valkey ok - %s", get_settings().valkey_url)


# --- 2/3. Postgres + schema ----------------------------------------------

async def _check_database() -> None:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        raise PreflightError(f"cannot connect to Postgres: {exc}") from exc
    log.info("postgres ok")


def _alembic_config():
    from alembic.config import Config

    # No ini file on purpose: that keeps alembic/env.py from re-running
    # logging.fileConfig() and muting the app loggers mid-startup.
    cfg = Config()
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    return cfg


def _alembic_upgrade_head() -> None:
    from alembic import command

    command.upgrade(_alembic_config(), "head")


def _alembic_head() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


async def _check_schema() -> None:
    settings = get_settings()
    if settings.preflight_run_migrations:
        try:
            await asyncio.to_thread(_alembic_upgrade_head)
        except Exception as exc:  # noqa: BLE001
            raise PreflightError(f"alembic upgrade head failed: {exc}") from exc
        log.info("migrations applied (alembic upgrade head)")

    async with engine.connect() as conn:
        current = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
        has_core_table = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).has_table("hosts")
        )

    if current is None:
        raise PreflightError(
            "database has no alembic_version - schema was never created "
            "(set OVC_PREFLIGHT_RUN_MIGRATIONS=true or run `alembic upgrade head`)"
        )
    head = _alembic_head()
    if head is not None and current != head:
        raise PreflightError(
            f"database is at revision {current}, expected head {head} - migrations pending"
        )
    if not has_core_table:
        raise PreflightError("core table 'hosts' is missing after migrations")
    log.info("schema ok - at head revision %s", current)


# --- 4. RabbitMQ -------------------------------------------------------

async def _check_rabbitmq() -> None:
    try:
        conn = await get_connection()
    except Exception as exc:  # noqa: BLE001
        raise PreflightError(f"cannot connect to RabbitMQ (AMQP): {exc}") from exc
    if conn.is_closed:
        raise PreflightError("RabbitMQ AMQP connection is closed")
    try:
        async with RabbitManagement() as mgmt:
            await mgmt.check_alive()
    except Exception as exc:  # noqa: BLE001
        raise PreflightError(f"RabbitMQ management API unhealthy: {exc}") from exc
    log.info("rabbitmq ok - %s", get_settings().rabbitmq_mgmt_url)


# --- 5. per-host queues / users / permissions -------------------------

async def _check_hosts_messaging() -> None:
    settings = get_settings()
    async with session_scope() as db:
        hosts = list((await db.execute(select(Host))).scalars().all())
        if not hosts:
            log.info("no hosts in database yet - skipping messaging validation")
            return

        channel = await get_channel()
        try:
            for host in hosts:
                await declare_host_queues(channel, host.short_id)  # idempotent repair
        finally:
            await channel.close()

        async with RabbitManagement() as mgmt:
            present = await mgmt.list_queue_names()
            missing = [
                queue_name(h.short_id, kind)
                for h in hosts
                for kind in ALL_KINDS
                if queue_name(h.short_id, kind) not in present
            ]
            if missing:
                raise PreflightError(
                    f"{len(missing)} host queue(s) still missing after declare: "
                    + ", ".join(missing[:8])
                    + (" …" if len(missing) > 8 else "")
                )

            if settings.preflight_manage_rabbitmq_users:
                for host in hosts:
                    ensure_host_agent_credentials(host)
                await db.flush()
                for host in hosts:
                    await ensure_host_agent_user(mgmt, host)
                    if await mgmt.get_permissions(host.agent_rmq_user) is None:
                        raise PreflightError(
                            f"host {host.id}: agent user {host.agent_rmq_user} has no "
                            "permissions on the vhost after provisioning"
                        )
        # session_scope commits any freshly generated agent credentials
    log.info("messaging ok - validated %d host(s)", len(hosts))


# --- runner ------------------------------------------------------------

def _steps() -> list[tuple[str, Callable[[], Awaitable[None]]]]:
    return [
        ("Valkey", _check_valkey),
        ("Postgres connection", _check_database),
        ("database schema", _check_schema),
        ("RabbitMQ", _check_rabbitmq),
        ("host messaging (queues / users / permissions)", _check_hosts_messaging),
    ]


async def run_preflight() -> None:
    settings = get_settings()
    if not settings.preflight:
        log.warning("preflight disabled (OVC_PREFLIGHT=false) - skipping startup checks")
        return

    log.info("startup preflight: running %d checks", len(_steps()))
    for label, step in _steps():
        try:
            await step()
        except PreflightError:
            raise
        except Exception as exc:  # noqa: BLE001 - any unexpected failure is fatal too
            raise PreflightError(f"{label}: {exc}") from exc
    log.info("startup preflight passed")
