"""Thin async client for the RabbitMQ HTTP management API.

Only what the startup preflight needs: health, listing queues, and upserting the
per-host agent user + its permissions. Credentials and vhost are taken from the
AMQP URL; the base URL is `OVC_RABBITMQ_MGMT_URL`.
"""
from __future__ import annotations

import logging
from urllib.parse import quote, unquote, urlsplit

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


class RabbitManagementError(RuntimeError):
    pass


def _amqp_parts() -> tuple[str, str, str]:
    """(username, password, vhost) from the configured AMQP URL."""
    parts = urlsplit(get_settings().rabbitmq_url)
    user = unquote(parts.username) if parts.username else "guest"
    password = unquote(parts.password) if parts.password else "guest"
    vhost = parts.path[1:] if parts.path.startswith("/") else parts.path
    return user, password, unquote(vhost) or "/"


class RabbitManagement:
    def __init__(self) -> None:
        settings = get_settings()
        user, password, vhost = _amqp_parts()
        self.vhost = vhost
        self._base = settings.rabbitmq_mgmt_url.rstrip("/")
        self._auth = (user, password)

    async def __aenter__(self) -> RabbitManagement:
        self._client = httpx.AsyncClient(
            base_url=self._base, auth=self._auth, timeout=10.0
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kw: object) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise RabbitManagementError(f"{method} {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise RabbitManagementError(
                f"{method} {path} -> {resp.status_code} {resp.text[:200]}"
            )
        return resp

    # --- health --------------------------------------------------------------

    async def check_alive(self) -> None:
        """Raise unless the broker answers /api/overview and the vhost passes
        the aliveness test (declare + publish + consume a throwaway message)."""
        await self._request("GET", "/api/overview")
        resp = await self._request(
            "GET", f"/api/aliveness-test/{quote(self.vhost, safe='')}"
        )
        if resp.json().get("status") != "ok":
            raise RabbitManagementError(
                f"aliveness-test failed on vhost {self.vhost!r}: {resp.text[:200]}"
            )

    # --- queues -------------------------------------------------------------

    async def list_queue_names(self) -> set[str]:
        resp = await self._request(
            "GET",
            f"/api/queues/{quote(self.vhost, safe='')}",
            params={"columns": "name", "disable_stats": "true"},
        )
        return {q["name"] for q in resp.json()}

    # --- users / permissions ----------------------------------------------

    async def user_exists(self, name: str) -> bool:
        resp = await self._client.get(f"/api/users/{quote(name, safe='')}", auth=self._auth)
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise RabbitManagementError(f"GET /api/users/{name} -> {resp.status_code}")
        return True

    async def upsert_user(self, name: str, password: str, tags: str = "") -> None:
        await self._request(
            "PUT",
            f"/api/users/{quote(name, safe='')}",
            json={"password": password, "tags": tags},
        )

    async def delete_user(self, name: str) -> None:
        """Remove a RabbitMQ user (and, with it, its vhost permissions). A user
        that is already gone is not an error."""
        resp = await self._client.delete(
            f"/api/users/{quote(name, safe='')}", auth=self._auth
        )
        if resp.status_code not in (204, 404):
            raise RabbitManagementError(
                f"DELETE /api/users/{name} -> {resp.status_code} {resp.text[:200]}"
            )

    async def set_permissions(
        self, name: str, *, configure: str, write: str, read: str
    ) -> None:
        await self._request(
            "PUT",
            f"/api/permissions/{quote(self.vhost, safe='')}/{quote(name, safe='')}",
            json={"configure": configure, "write": write, "read": read},
        )

    async def get_permissions(self, name: str) -> dict | None:
        resp = await self._client.get(
            f"/api/permissions/{quote(self.vhost, safe='')}/{quote(name, safe='')}",
            auth=self._auth,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RabbitManagementError(f"GET permissions/{name} -> {resp.status_code}")
        return resp.json()
