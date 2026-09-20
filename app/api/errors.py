from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Raise anywhere in a request to produce the `{ error: {...} }` envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def not_found(what: str) -> ApiError:
    return ApiError("NOT_FOUND", f"{what} not found", status.HTTP_404_NOT_FOUND)


def forbidden(message: str = "You do not have access to this resource") -> ApiError:
    return ApiError("FORBIDDEN", message, status.HTTP_403_FORBIDDEN)


def vm_locked(vm_name: str, lock: dict) -> ApiError:
    return ApiError(
        "VM_LOCKED",
        f"'{vm_name}' is busy - a {lock.get('kind', 'operation')} is already "
        f"running on it. Wait for it to finish before trying again.",
        status.HTTP_409_CONFLICT,
        details={"lock": lock},
    )


def unauthorized(message: str = "Authentication required") -> ApiError:
    return ApiError("UNAUTHENTICATED", message, status.HTTP_401_UNAUTHORIZED)


def _envelope(code: str, message: str, details: dict | None = None) -> dict:
    body: dict = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "UNAUTHENTICATED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
        }.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(
                "VALIDATION_ERROR", "Request validation failed", {"errors": exc.errors()}
            ),
        )

    @app.exception_handler(DBAPIError)
    async def _db_error(_: Request, exc: DBAPIError) -> JSONResponse:
        # a path/query id that isn't a valid UUID reaches asyncpg as a bad bind
        # param - treat it as "no such resource" rather than a 500
        detail = str(getattr(exc, "orig", exc)).lower()
        if "invalid uuid" in detail or ("invalid input for query" in detail and "uuid" in detail):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=_envelope("NOT_FOUND", "Resource not found"),
            )
        log.exception("database error: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("INTERNAL", "Internal server error"),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("INTERNAL", "Internal server error"),
        )
