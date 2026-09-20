from __future__ import annotations

import os

import uvicorn

from .config import get_settings


def run_api() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=os.getenv("OVC_HOST", "0.0.0.0"),
        port=int(os.getenv("OVC_PORT", "8000")),
        reload=settings.is_dev and os.getenv("OVC_RELOAD", "1") == "1",
        log_config=None,
    )


if __name__ == "__main__":
    run_api()
