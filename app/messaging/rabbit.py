from __future__ import annotations

import logging

import aio_pika

from ..config import get_settings

log = logging.getLogger(__name__)
_settings = get_settings()

_connection: aio_pika.abc.AbstractRobustConnection | None = None


async def get_connection() -> aio_pika.abc.AbstractRobustConnection:
    global _connection
    if _connection is None or _connection.is_closed:
        log.info("connecting to RabbitMQ")
        _connection = await aio_pika.connect_robust(_settings.rabbitmq_url)
    return _connection


async def get_channel() -> aio_pika.abc.AbstractChannel:
    conn = await get_connection()
    return await conn.channel()


async def close_connection() -> None:
    global _connection
    if _connection is not None and not _connection.is_closed:
        await _connection.close()
    _connection = None


async def rabbit_healthy() -> bool:
    try:
        conn = await get_connection()
        return not conn.is_closed
    except Exception:  # noqa: BLE001 - health check must not raise
        return False
