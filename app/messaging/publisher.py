from __future__ import annotations

import logging

import aio_pika

from .protocol import AgentRequest
from .queues import QueueKind, queue_name
from .rabbit import get_channel

log = logging.getLogger(__name__)


async def publish_request(host_ref: str, request: AgentRequest) -> None:
    """Write a JSON request to <host_ref>.request (host_ref = host short_id).
    `request.id` becomes the AMQP correlation id so responses can be matched
    back to the Task."""
    channel = await get_channel()
    try:
        await channel.declare_queue(
            queue_name(host_ref, QueueKind.REQUEST), durable=True
        )
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=request.model_dump_json().encode(),
                content_type="application/json",
                correlation_id=request.id,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=queue_name(host_ref, QueueKind.REQUEST),
        )
        log.info("published %s for host=%s task=%s", request.function, host_ref, request.id)
    finally:
        await channel.close()
