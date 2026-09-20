from __future__ import annotations

import os

import pytest

from app.services.agent_store import (
    AgentStoreError,
    LocalAgentStore,
    object_key,
    sign_download,
    sign_scope,
    verify_download,
    verify_scope,
)


def test_download_token_roundtrip() -> None:
    q = sign_download("bin-1", ttl_seconds=60)
    assert verify_download("bin-1", q["exp"], q["token"])
    # wrong binary id / tampered token / expired all fail
    assert not verify_download("bin-2", q["exp"], q["token"])
    assert not verify_download("bin-1", q["exp"], q["token"] + "0")
    assert not verify_download("bin-1", "1", q["token"])
    assert not verify_download("bin-1", None, None)


def test_scope_token_roundtrip() -> None:
    q = sign_scope("host-install:h1", 60)
    assert verify_scope("host-install:h1", q["exp"], q["token"])
    # wrong scope / tampered token / expired / missing all fail
    assert not verify_scope("host-install:h2", q["exp"], q["token"])
    assert not verify_scope("host-install:h1", q["exp"], q["token"] + "0")
    assert not verify_scope("host-install:h1", "1", q["token"])
    assert not verify_scope("host-install:h1", None, None)
    # a download token can't be replayed as a scope token
    d = sign_download("host-install:h1", ttl_seconds=60)
    assert not verify_scope("host-install:h1", d["exp"], d["token"])


async def test_local_store_roundtrip(tmp_path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"ovc-agent" * 4096)
    store = LocalAgentStore(str(tmp_path / "store"))
    key = object_key("bin-1", "ovc-agent.exe")

    await store.put_file(key, str(src), content_type="application/octet-stream")
    got = b"".join([chunk async for chunk in store.open_stream(key)])
    assert got == src.read_bytes()

    await store.delete(key)
    with pytest.raises(AgentStoreError):
        async for _ in store.open_stream(key):
            pass


async def test_local_store_rejects_traversal(tmp_path) -> None:
    store = LocalAgentStore(str(tmp_path))
    with pytest.raises(AgentStoreError):
        async for _ in store.open_stream("../../etc/passwd"):
            pass


def test_object_key() -> None:
    assert object_key("abc", "ovc-agent.exe") == "abc/ovc-agent.exe"
    assert os.path.basename(object_key("abc", "ovc-agent.exe")) == "ovc-agent.exe"
