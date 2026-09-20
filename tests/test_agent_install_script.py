from __future__ import annotations

from types import SimpleNamespace

from app.services.hosts import render_agent_install_script


def _script(**over: object) -> str:
    host = SimpleNamespace(name="HV-01", short_id="ab12cd")
    kw: dict[str, object] = {
        "download_url": "http://ovc.local/api/agent-binaries/x/download?exp=1&token=a",
        "checksum_sha256": "deadbeef",
        "version": "1.2.3",
        "config_ini": "[agent]\nhost_id = ab12cd\ntemplate_path = D:\\HyperV\n",
    }
    kw.update(over)
    return render_agent_install_script(host, **kw)  # type: ignore[arg-type]


def test_script_has_the_five_steps() -> None:
    s = _script()
    assert "New-Item -ItemType Directory -Force -Path $InstallDir" in s
    assert "Invoke-WebRequest -Uri $DownloadUrl" in s
    assert "Set-Content -Path $ConfigPath" in s
    assert "& $ExePath install" in s
    assert "Start-Process notepad.exe $ConfigPath" in s
    # tells the operator how to start the service
    assert "Start-Service $ServiceName" in s
    assert "$ServiceName = 'ovc-agent'" in s


def test_checksum_is_upper_cased_for_get_filehash() -> None:
    assert "$Checksum    = 'DEADBEEF'" in _script(checksum_sha256="deadbeef")


def test_config_ini_embedded_as_literal_here_string() -> None:
    s = _script(config_ini="[agent]\nfoo = bar\n")
    assert "$ConfigBody = @'\n[agent]\nfoo = bar\n'@" in s


def test_here_string_terminator_inside_config_is_defused() -> None:
    s = _script(config_ini="[agent]\n'@ = evil\n")
    # a line starting with '@ would otherwise close the here-string early
    assert "\n '@ = evil\n" in s
