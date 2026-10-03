from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from app.api.deps import get_scope
from app.api.errors import ApiError
from app.config import Settings
from app.db import get_session
from app.ids import new_id
from app.main import app
from app.models import (
    AgentBinary,
    AuditEvent,
    Cluster,
    Folder,
    Host,
    Tag,
    TagCategory,
    Task,
    User,
    Vlan,
    Vm,
)
from app.services import audit
from app.services.rbac import VisibleScope


class FakeSession:
    """Just enough AsyncSession for `audit.record` / `prune_events`."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def execute(self, *_: object) -> None:
        raise AssertionError("no query expected")


def _user() -> User:
    return User(id=new_id(), email="ana@corp.test", display_name="Ana")


# ---- changes ------------------------------------------------------------------

def test_changes_keeps_only_changed_keys() -> None:
    diff = audit.changes({"name": "a", "fqdn": "x"}, {"name": "b", "fqdn": "x"})
    assert diff == {"before": {"name": "a"}, "after": {"name": "b"}}


def test_changes_none_when_equal() -> None:
    assert audit.changes({"folder": None}, {"folder": None}) is None


# ---- record ---------------------------------------------------------------------

async def test_record_user_event_captures_target_and_context() -> None:
    db = FakeSession()
    host_id, cluster_id = new_id(), new_id()
    vm = Vm(id=new_id(), name="web01", host_id=host_id, cluster_id=cluster_id)

    event = await audit.record(db, _user(), "vm.move_folder", vm, details={"a": 1})

    assert db.added == [event]
    assert (event.actor_type, event.actor_email, event.actor_name) == (
        "user",
        "ana@corp.test",
        "Ana",
    )
    assert (event.target_type, event.target_id, event.target_name) == ("vm", vm.id, "web01")
    assert (event.host_id, event.cluster_id) == (host_id, cluster_id)
    assert event.outcome == "succeeded"
    assert event.task_id is None
    assert event.details == {"a": 1}


async def test_record_task_event_starts_pending() -> None:
    db = FakeSession()
    host = Host(id=new_id(), name="hv01", cluster_id=None)
    task = Task(id=new_id())

    event = await audit.record(db, _user(), "host.restart", host, task=task)

    assert event.task_id == task.id
    assert event.outcome == "pending"
    assert (event.host_id, event.cluster_id) == (host.id, None)


async def test_record_system_event() -> None:
    db = FakeSession()
    vm = Vm(id=new_id(), name="db01", host_id=new_id(), cluster_id=None)

    event = await audit.record(db, None, "vm.inventory_remove", vm)

    assert event.actor_type == "system"
    assert event.actor_email is None
    assert event.actor_name == audit.SYSTEM_ACTOR_NAME


async def test_record_without_target_needs_target_type() -> None:
    db = FakeSession()
    event = await audit.record(db, _user(), "vm.lock_release_all", target_type="vm")
    assert (event.target_type, event.target_id) == ("vm", None)

    with pytest.raises(ValueError):
        await audit.record(db, _user(), "vm.lock_release_all")


async def test_record_user_without_display_name_falls_back_to_email() -> None:
    user = User(id=new_id(), email="bob@corp.test", display_name=None)
    event = await audit.record(FakeSession(), user, "tag.create", Tag(id=new_id(), name="x"))
    assert event.actor_name == "bob@corp.test"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (Cluster(id="c1", name="prod"), ("cluster", "prod", None, "c1")),
        (Folder(id="f1", name="web", host_id=None, cluster_id="c1"), ("folder", "web", None, "c1")),
        (Vlan(id="v1", name="dmz", host_id="h1", cluster_id=None), ("vlan", "dmz", "h1", None)),
        (Tag(id="t1", name="linux"), ("tag", "linux", None, None)),
        (TagCategory(id="tc1", name="os"), ("tag_category", "os", None, None)),
        (AgentBinary(id="b1", version="0.3.0"), ("agent_binary", "0.3.0", None, None)),
    ],
)
async def test_record_target_types(target: audit.Target, expected: tuple) -> None:
    event = await audit.record(FakeSession(), _user(), "x.y", target)  # type: ignore[arg-type]
    assert (event.target_type, event.target_name, event.host_id, event.cluster_id) == expected


async def test_prune_zero_keeps_everything() -> None:
    assert await audit.prune_events(FakeSession(), 0) == 0  # type: ignore[arg-type]


# ---- cursor -------------------------------------------------------------------

def test_cursor_round_trip() -> None:
    event = AuditEvent(id=new_id(), occurred_at=datetime(2026, 10, 3, 12, 0, 1, 5, tzinfo=UTC))
    assert audit.decode_cursor(audit.encode_cursor(event)) == (event.occurred_at, event.id)


@pytest.mark.parametrize("cursor", ["", "not-base64!", "Zm9v", "MjAyNi0xMC0wM3xub3QtYS11dWlk"])
def test_invalid_cursor(cursor: str) -> None:
    with pytest.raises(ApiError) as exc:
        audit.decode_cursor(cursor)
    assert exc.value.code == "INVALID"


# ---- settings -------------------------------------------------------------------

def test_retention_default_and_bounds() -> None:
    assert Settings().audit_retention_days == 365
    assert Settings(audit_retention_days=0).audit_retention_days == 0
    with pytest.raises(ValidationError):
        Settings(audit_retention_days=-1)


# ---- route ----------------------------------------------------------------------

@pytest.fixture
def client_as():
    """An ASGI client whose caller has the given scope; no DB is touched."""

    async def no_session():
        yield None

    def make(scope: VisibleScope) -> httpx.AsyncClient:
        app.dependency_overrides[get_session] = no_session
        app.dependency_overrides[get_scope] = lambda: scope
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    yield make
    app.dependency_overrides.clear()


async def test_audit_events_admin_only(client_as) -> None:
    async with client_as(VisibleScope(all=False, host_ids={"h1"})) as client:
        resp = await client.get("/api/audit-events")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_audit_events_rejects_bad_ids(client_as) -> None:
    async with client_as(VisibleScope(all=True)) as client:
        resp = await client.get("/api/audit-events", params={"hostId": "nope"})
    assert resp.status_code == 422
