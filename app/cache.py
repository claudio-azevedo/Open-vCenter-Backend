from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis

from .config import get_settings
from .task_timeouts import task_timeout_seconds

_settings = get_settings()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()

_client: redis.Redis | None = None


def get_cache() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(_settings.valkey_url, decode_responses=True)
    return _client


# ---- VM state overlay ---------------------------------------------------------
# The worker writes the freshest VM state here the moment an agent responds, so the
# API doesn't have to wait for the next periodic vm_inventory. Read paths overlay
# this on top of the DB row.

def _vm_state_key(vm_id: str) -> str:
    return f"vm:state:{vm_id}"


async def set_vm_state(vm_id: str, state: str) -> None:
    await get_cache().set(
        _vm_state_key(vm_id), state, ex=_settings.vm_state_ttl_seconds
    )


async def get_vm_state(vm_id: str) -> str | None:
    return await get_cache().get(_vm_state_key(vm_id))


async def get_vm_states(vm_ids: list[str]) -> dict[str, str]:
    if not vm_ids:
        return {}
    keys = [_vm_state_key(v) for v in vm_ids]
    values = await get_cache().mget(keys)
    return {vm_id: v for vm_id, v in zip(vm_ids, values, strict=True) if v}


async def clear_vm_state(vm_id: str) -> None:
    await get_cache().delete(_vm_state_key(vm_id))


# ---- VM lock ----------------------------------------------------------------
# A VM is locked for the lifetime of a mutating agent task (power, edit, disk,
# snapshot, clone, …) so a second operation can't race it. The lock auto-expires
# a minute after the task-timeout deadline, so a crashed worker never strands it;
# `apply_response` / the timeout sweeper release it explicitly the moment the
# task finishes.

def _vm_lock_key(vm_id: str) -> str:
    return f"vm:lock:{vm_id}"


# compare-and-delete: only drop the lock if it is still held by *this* task, so a
# late release from an old task can't free a newer one.
_RELEASE_LUA = """
local v = redis.call('get', KEYS[1])
if not v then return 0 end
local ok, obj = pcall(cjson.decode, v)
if ok and obj.task_id == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


def _lock_ttl(kind: str | None = None) -> int:
    # Backstop only - apply_response / the timeout sweeper free the lock the
    # moment the task ends. Sized per kind so a legitimately long task (a
    # graceful shutdown, a storage migration) never outlives its own lock.
    return task_timeout_seconds(kind) + 60


async def acquire_vm_lock(
    vm_id: str, *, task_id: str, kind: str, requested_by: str
) -> bool:
    """Take the lock for `vm_id`. Returns False if another task already holds it."""
    payload = json.dumps(
        {
            "task_id": task_id,
            "kind": kind,
            "requested_by": requested_by,
            "acquired_at": _now_iso(),
        }
    )
    ok = await get_cache().set(
        _vm_lock_key(vm_id), payload, ex=_lock_ttl(kind), nx=True
    )
    return bool(ok)


async def release_vm_lock(vm_id: str, task_id: str | None = None) -> bool:
    """Release the lock. With `task_id`, only if that task still holds it;
    without, unconditionally (admin force-release)."""
    cache = get_cache()
    if task_id is None:
        return bool(await cache.delete(_vm_lock_key(vm_id)))
    return bool(await cache.eval(_RELEASE_LUA, 1, _vm_lock_key(vm_id), task_id))


async def get_vm_lock(vm_id: str) -> dict[str, Any] | None:
    raw = await get_cache().get(_vm_lock_key(vm_id))
    return json.loads(raw) if raw else None


async def get_vm_locks(vm_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not vm_ids:
        return {}
    values = await get_cache().mget([_vm_lock_key(v) for v in vm_ids])
    return {
        vm_id: json.loads(v)
        for vm_id, v in zip(vm_ids, values, strict=True)
        if v
    }


async def list_vm_locks() -> list[dict[str, Any]]:
    cache = get_cache()
    out: list[dict[str, Any]] = []
    async for key in cache.scan_iter(match="vm:lock:*"):
        raw = await cache.get(key)
        if not raw:
            continue
        info = json.loads(raw)
        info["vm_id"] = key.split(":", 2)[2]
        info["ttl"] = await cache.ttl(key)
        out.append(info)
    return out


async def release_all_vm_locks() -> int:
    cache = get_cache()
    keys = [key async for key in cache.scan_iter(match="vm:lock:*")]
    return int(await cache.delete(*keys)) if keys else 0


# ---- recently-deleted VM tombstone ----------------------------------------
# A successful vm_delete drops the DB row immediately, but a periodic
# vm_inventory snapshot the agent captured *before* the deletion can still be
# sitting on the last-value queue and would resurrect the row when the worker
# consumes it. We remember a deleted VM's Hyper-V GUID for a short window;
# apply_vm_inventory ignores that GUID until a fresh snapshot (which no longer
# lists it) arrives and clears the tombstone.

# Comfortably longer than the agent's default refresh_interval_vms (300s) so at
# least one fresh snapshot always lands while the tombstone is live.
_VM_TOMBSTONE_TTL = 900


def _vm_tombstone_key(host_id: str, vm_uuid: str) -> str:
    return f"vm:deleted:{host_id}:{vm_uuid}"


async def mark_vm_deleted(host_id: str, vm_uuid: str) -> None:
    if not vm_uuid:
        return
    await get_cache().set(
        _vm_tombstone_key(host_id, vm_uuid), _now_iso(), ex=_VM_TOMBSTONE_TTL
    )


async def is_vm_deleted(host_id: str, vm_uuid: str) -> bool:
    return bool(await get_cache().exists(_vm_tombstone_key(host_id, vm_uuid)))


async def clear_vm_deleted(host_id: str, vm_uuid: str) -> None:
    await get_cache().delete(_vm_tombstone_key(host_id, vm_uuid))


async def list_deleted_vm_uuids(host_id: str) -> set[str]:
    cache = get_cache()
    prefix = f"vm:deleted:{host_id}:"
    return {
        key[len(prefix):]
        async for key in cache.scan_iter(match=f"{prefix}*")
    }


# ---- generic last-value cache (agent_status etc.) ---------------------------

async def put_json(key: str, value: Any, ttl: int | None = None) -> None:
    await get_cache().set(key, json.dumps(value), ex=ttl)


async def get_json(key: str) -> Any | None:
    raw = await get_cache().get(key)
    return json.loads(raw) if raw else None
