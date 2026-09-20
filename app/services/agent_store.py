"""Where uploaded ovc-agent binaries live.

Two backends, chosen by ``OVC_AGENT_STORAGE``:

* ``local`` - a directory mounted into the API/worker containers (a k8s PVC, a
  docker volume). The agent downloads the binary from this backend over plain
  HTTP; the URL carries a short-lived HMAC token (the agent can't send an
  ``Authorization`` header).
* ``s3`` - any S3-compatible object store. The agent downloads straight from a
  presigned GET URL, so the backend never proxies the bytes.

The agent's downloader (``ovc-agent-hyperv/internal/download/downloader.go``)
does a plain ``GET``, follows redirects, and needs a 2xx - both URL shapes
satisfy that.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import os
import time
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import aiofiles

from ..config import get_settings

log = logging.getLogger(__name__)

_CHUNK = 1 << 20  # 1 MiB


class AgentStoreError(Exception):
    pass


def object_key(binary_id: str, filename: str) -> str:
    return f"{binary_id}/{filename}"


# --- local-mode download token ------------------------------------------------
#
# {public_base_url}{api_prefix}/agent-binaries/{id}/download?exp=<epoch>&token=<hmac>
# token = HMAC-SHA256(session_secret, f"{binary_id}.{exp}")


def sign_download(binary_id: str, ttl_seconds: int | None = None) -> dict[str, str]:
    settings = get_settings()
    ttl = ttl_seconds or settings.agent_download_url_ttl_seconds
    exp = int(time.time()) + ttl
    return {"exp": str(exp), "token": _download_mac(binary_id, exp)}


def verify_download(binary_id: str, exp: str | None, token: str | None) -> bool:
    if not exp or not token:
        return False
    try:
        exp_i = int(exp)
    except ValueError:
        return False
    if exp_i < int(time.time()):
        return False
    return hmac.compare_digest(token, _download_mac(binary_id, exp_i))


def _download_mac(binary_id: str, exp: int) -> str:
    return _hmac_hex(f"{binary_id}.{exp}")


def _hmac_hex(msg: str) -> str:
    secret = get_settings().session_secret.encode()
    return hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()


# --- generic scoped URL token ------------------------------------------------
#
# Same HMAC construction as the download token, but keyed by an arbitrary scope
# string (e.g. ``host-install:<host_id>``) so one signer covers unauthenticated
# GET endpoints a browser session can't reach (a PowerShell one-liner on a host).


def sign_scope(scope: str, ttl_seconds: int) -> dict[str, str]:
    exp = int(time.time()) + ttl_seconds
    return {"exp": str(exp), "token": _hmac_hex(f"scope:{scope}.{exp}")}


def verify_scope(scope: str, exp: str | None, token: str | None) -> bool:
    if not exp or not token:
        return False
    try:
        exp_i = int(exp)
    except ValueError:
        return False
    if exp_i < int(time.time()):
        return False
    return hmac.compare_digest(token, _hmac_hex(f"scope:{scope}.{exp_i}"))


# --- store interface ---------------------------------------------------------


class AgentBinaryStore(Protocol):
    backend: str

    async def put_file(self, key: str, local_path: str, *, content_type: str) -> None: ...

    def open_stream(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete(self, key: str) -> None: ...

    async def download_url(self, binary_id: str, key: str, filename: str) -> str | None:
        """The URL an agent GETs, or None when downloads are not available
        (local mode without OVC_PUBLIC_BASE_URL)."""
        ...

    def describe(self) -> str:
        """Human-readable location, for the read-only storage panel."""
        ...


# --- local -----------------------------------------------------------------


# repo root - parent of the `app` package. Used to anchor a *relative*
# OVC_AGENT_STORAGE_DIR so it doesn't depend on the process working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class LocalAgentStore:
    backend = "local"

    def __init__(self, root: str) -> None:
        p = Path(root).expanduser()
        # An absolute path (e.g. "/app/uploads", a mounted volume) is used as-is.
        # A relative path ("uploads", "var/agent-binaries") is resolved against
        # the repo/app root, not the CWD.
        self.root = (p if p.is_absolute() else _REPO_ROOT / p).resolve()

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root)):
            raise AgentStoreError("path traversal in object key")
        return p

    async def put_file(self, key: str, local_path: str, *, content_type: str) -> None:
        dst = self._path(key)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + f".{os.getpid()}.tmp")
            async with (
                aiofiles.open(local_path, "rb") as src,
                aiofiles.open(tmp, "wb") as out,
            ):
                while chunk := await src.read(_CHUNK):
                    await out.write(chunk)
            os.replace(tmp, dst)
        except OSError as exc:
            raise AgentStoreError(
                f"cannot write to agent storage dir {self.root}: {exc}"
            ) from exc

    async def open_stream(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        if not path.is_file():
            raise AgentStoreError(f"agent binary not found: {key}")
        async with aiofiles.open(path, "rb") as f:
            while chunk := await f.read(_CHUNK):
                yield chunk

    async def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AgentStoreError(f"cannot delete {key}: {exc}") from exc
        # drop the now-empty "<id>/" dir (keys are "<id>/<filename>")
        parent = path.parent
        if parent != self.root:
            with contextlib.suppress(OSError):
                parent.rmdir()

    async def download_url(self, binary_id: str, key: str, filename: str) -> str | None:
        settings = get_settings()
        base = settings.public_base_url.rstrip("/")
        if not base:
            return None
        qs = urlencode(sign_download(binary_id))
        return f"{base}{settings.api_prefix}/agent-binaries/{binary_id}/download?{qs}"

    def describe(self) -> str:
        return str(self.root)


# --- s3 --------------------------------------------------------------------


class S3AgentStore:
    backend = "s3"

    def __init__(
        self, bucket: str, prefix: str, *, endpoint_url: str, region: str
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._endpoint_url = endpoint_url or None
        self._region = region

    def _s3_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _client(self):
        import aioboto3

        session = aioboto3.Session()
        return session.client(
            "s3", endpoint_url=self._endpoint_url, region_name=self._region
        )

    async def put_file(self, key: str, local_path: str, *, content_type: str) -> None:
        try:
            async with self._client() as s3, aiofiles.open(local_path, "rb") as f:
                await s3.put_object(
                    Bucket=self.bucket,
                    Key=self._s3_key(key),
                    Body=await f.read(),
                    ContentType=content_type,
                )
        except Exception as exc:  # botocore ClientError / BotoCoreError
            raise AgentStoreError(f"S3 put failed: {exc}") from exc

    async def open_stream(self, key: str) -> AsyncIterator[bytes]:
        try:
            async with self._client() as s3:
                resp = await s3.get_object(Bucket=self.bucket, Key=self._s3_key(key))
                async with resp["Body"] as body:
                    async for chunk in body.iter_chunks(_CHUNK):
                        yield chunk
        except AgentStoreError:
            raise
        except Exception as exc:
            raise AgentStoreError(f"S3 get failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            async with self._client() as s3:
                await s3.delete_object(Bucket=self.bucket, Key=self._s3_key(key))
        except Exception as exc:
            raise AgentStoreError(f"S3 delete failed: {exc}") from exc

    async def download_url(self, binary_id: str, key: str, filename: str) -> str | None:
        ttl = get_settings().agent_download_url_ttl_seconds
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": self._s3_key(key),
                    "ResponseContentDisposition": f'attachment; filename="{filename}"',
                },
                ExpiresIn=ttl,
            )

    def describe(self) -> str:
        loc = f"{self.bucket}/{self.prefix}" if self.prefix else self.bucket
        return f"{loc} ({self._endpoint_url or 'aws'})"


@lru_cache
def get_agent_store() -> AgentBinaryStore:
    settings = get_settings()
    if settings.agent_storage == "s3":
        if not settings.agent_s3_bucket:
            raise AgentStoreError("OVC_AGENT_S3_BUCKET is required when OVC_AGENT_STORAGE=s3")
        return S3AgentStore(
            settings.agent_s3_bucket,
            settings.agent_s3_prefix,
            endpoint_url=settings.agent_s3_endpoint_url,
            region=settings.agent_s3_region,
        )
    return LocalAgentStore(settings.agent_storage_dir)
