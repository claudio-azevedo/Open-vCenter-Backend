from __future__ import annotations

import asyncio

from .main import run


def run_worker() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    run_worker()
