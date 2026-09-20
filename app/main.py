from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

from .api.errors import install_error_handlers
from .api.routes import api_router
from .config import get_settings
from .logging import configure_logging, install_request_logging
from .messaging import close_connection
from .preflight import PreflightError, run_preflight

log = logging.getLogger("ovc.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    settings = get_settings()
    log.info("starting ovc-backend API (env=%s auth=%s)", settings.env, settings.auth_mode)

    # Verify (and repair) Valkey / Postgres / RabbitMQ before serving; bail out otherwise.
    try:
        await run_preflight()
    except PreflightError as exc:
        log.critical("STARTUP PREFLIGHT FAILED - %s", exc)
        raise SystemExit(1) from exc

    yield

    await close_connection()
    log.info("ovc-backend API stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Open vCenter API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # DEV: log the JSON body of every mutating request to the console
    install_request_logging(app)

    install_error_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    # Same docs also served under the API prefix, so deployments that only route
    # `/api/*` to the backend still reach them.
    prefix = settings.api_prefix

    @app.get(f"{prefix}/openapi.json", include_in_schema=False)
    async def api_openapi():  # noqa: D401
        return app.openapi()

    @app.get(f"{prefix}/docs", include_in_schema=False)
    async def api_docs():
        return get_swagger_ui_html(
            openapi_url=f"{prefix}/openapi.json", title=f"{app.title} - Swagger UI"
        )

    @app.get(f"{prefix}/redoc", include_in_schema=False)
    async def api_redoc():
        return get_redoc_html(
            openapi_url=f"{prefix}/openapi.json", title=f"{app.title} - ReDoc"
        )

    return app


app = create_app()
