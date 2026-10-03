from __future__ import annotations

import base64

from app.messaging import AgentResponse
from app.models import Vm
from app.services.inventory import (
    MAX_THUMBNAIL_BYTES,
    _pop_thumbnail,
    _strip_response_thumbnails,
    _write_vm_row,
    normalize_vm,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_pop_thumbnail_decodes_and_removes_field() -> None:
    raw = {"id": "g1", "thumbnail": _b64(JPEG)}
    assert _pop_thumbnail(raw) == JPEG
    assert "thumbnail" not in raw


def test_pop_thumbnail_rejects_invalid_payloads() -> None:
    assert _pop_thumbnail({"id": "g1"}) is None
    assert _pop_thumbnail({"id": "g1", "thumbnail": "not base64!"}) is None
    assert _pop_thumbnail({"id": "g1", "thumbnail": _b64(b"PNG...")}) is None
    oversized = JPEG + b"\x00" * MAX_THUMBNAIL_BYTES
    raw = {"id": "g1", "thumbnail": _b64(oversized)}
    assert _pop_thumbnail(raw) is None
    assert "thumbnail" not in raw


def _apply(vm: Vm, raw: dict, *, partial: bool = False) -> None:
    _write_vm_row(vm, raw, normalize_vm(raw), host_id="h1", cluster_id=None, partial=partial)


def test_guest_os_kept_when_vm_stops_reporting_it() -> None:
    vm = Vm(id="v1", host_id="h1", vm_uuid="g1")
    _apply(vm, {"id": "g1", "name": "web", "state": "Running", "guestOs": " Windows 11 "})
    assert vm.guest_os == "Windows 11"

    # Off VM: the agent omits guestOs (or sends a blank) - keep the last value.
    _apply(vm, {"id": "g1", "name": "web", "state": "Off"})
    _apply(vm, {"id": "g1", "name": "web", "state": "Off", "guestOs": "  "})
    assert vm.guest_os == "Windows 11"

    _apply(vm, {"id": "g1", "state": "Running", "guestOs": "Ubuntu"}, partial=True)
    assert vm.guest_os == "Ubuntu"


def test_strip_response_thumbnails() -> None:
    resp = AgentResponse.model_validate(
        {
            "id": "t1",
            "function": "host_management",
            "status": "succeeded",
            "result": {"vms": [{"id": "g1", "thumbnail": "x"}, {"id": "g2"}]},
            "vm_status": {"id": "g1", "thumbnail": "x"},
        }
    )
    _strip_response_thumbnails(resp)
    dumped = resp.model_dump(mode="json")
    assert "thumbnail" not in dumped["vm_status"]
    assert all("thumbnail" not in v for v in dumped["result"]["vms"])
