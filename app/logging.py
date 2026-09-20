from __future__ import annotations

import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from .config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler()

    if settings.log_json:
        from pythonjsonlogger.json import JsonFormatter

        handler.setFormatter(
            JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())

    # quiet noisy libraries a notch
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)


_req_log = logging.getLogger("ovc.api.request")

# keys whose values are replaced with "***" before a body is logged
_REDACT_KEYS = {
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "client_secret",
    "session_secret",
    "api_key",
}
_MAX_BODY = 16_384


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower() in _REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class RequestBodyLogMiddleware(BaseHTTPMiddleware):
    """DEV only: log the method, path and (JSON) body of mutating requests so the
    payload the frontend actually posted is visible in the console."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            raw = await request.body()
            # re-feed the body so the downstream handler can still read it
            request._receive = _replay(raw)
            if raw:
                self._log(request, raw)
        return await call_next(request)

    @staticmethod
    def _log(request: Request, raw: bytes) -> None:
        body = raw[:_MAX_BODY]
        truncated = " …(truncated)" if len(raw) > _MAX_BODY else ""
        ctype = request.headers.get("content-type", "")
        rendered = body.decode("utf-8", "replace")
        if "application/json" in ctype:
            try:
                rendered = json.dumps(
                    _redact(json.loads(body)), indent=2, ensure_ascii=False
                )
            except ValueError:
                pass
        _req_log.info(
            "%s %s%s\n%s%s",
            request.method,
            request.url.path,
            f"?{request.url.query}" if request.url.query else "",
            rendered,
            truncated,
        )


def _replay(body: bytes):
    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def install_request_logging(app) -> None:
    """Wire the DEV request-body logger (no-op outside env=dev)."""
    if get_settings().is_dev:
        app.add_middleware(RequestBodyLogMiddleware)
