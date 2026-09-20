from __future__ import annotations

from enum import StrEnum

import aio_pika

from ..config import get_settings

_settings = get_settings()


class QueueKind(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"
    AGENT_STATUS = "agent_status"
    VM_INVENTORY = "vm_inventory"
    HOST_INVENTORY = "host_inventory"
    TEMPLATE_INVENTORY = "template_inventory"
    ISO_INVENTORY = "iso_inventory"
    # short time-series queues (NOT last-value): host / VM quick metrics
    VM_METRICS = "vm_metrics"
    HOST_METRICS = "host_metrics"


# Queues the worker consumes (backend <- agent).
CONSUMED_KINDS: tuple[QueueKind, ...] = (
    QueueKind.RESPONSE,
    QueueKind.AGENT_STATUS,
    QueueKind.VM_INVENTORY,
    QueueKind.HOST_INVENTORY,
    QueueKind.TEMPLATE_INVENTORY,
    QueueKind.ISO_INVENTORY,
    QueueKind.VM_METRICS,
    QueueKind.HOST_METRICS,
)

# "last message only" queues - keep just the newest payload.
LAST_VALUE_KINDS: frozenset[QueueKind] = frozenset(
    {
        QueueKind.AGENT_STATUS,
        QueueKind.VM_INVENTORY,
        QueueKind.HOST_INVENTORY,
        QueueKind.TEMPLATE_INVENTORY,
        QueueKind.ISO_INVENTORY,
    }
)

ALL_KINDS: tuple[QueueKind, ...] = tuple(QueueKind)


# `host_ref` throughout this module is the host `short_id` (app.ids.new_host_short_id),
# never the UUID PK - it is what appears in queue names and agent permissions.
def queue_name(host_ref: str, kind: QueueKind) -> str:
    return f"{host_ref}.{kind.value}"


def _queue_args(kind: QueueKind) -> dict:
    if kind in LAST_VALUE_KINDS:
        return {
            "x-max-length": _settings.inventory_queue_max_length,
            "x-overflow": "drop-head",
        }
    return {}


async def declare_host_queues(
    channel: aio_pika.abc.AbstractChannel, host_ref: str
) -> dict[QueueKind, aio_pika.abc.AbstractQueue]:
    """Idempotently declare every queue a host needs. Called on startup and on
    host creation."""
    queues: dict[QueueKind, aio_pika.abc.AbstractQueue] = {}
    for kind in ALL_KINDS:
        queues[kind] = await channel.declare_queue(
            queue_name(host_ref, kind),
            durable=True,
            arguments=_queue_args(kind) or None,
        )
    return queues


async def delete_host_queues(
    channel: aio_pika.abc.AbstractChannel, host_ref: str
) -> None:
    """Remove every queue a host owns. Called on host deletion; a queue that is
    already gone is not an error."""
    for kind in ALL_KINDS:
        await channel.queue_delete(queue_name(host_ref, kind))


_QUEUE_BODY = r"(request|response|agent_status|.*_inventory|.*_metrics)"


def agent_permission_pattern(host_ref: str) -> str:
    """RabbitMQ configure/read regex so a host's agent user can only declare and
    consume its own queues."""
    return f"^{host_ref}\\.{_QUEUE_BODY}$"


def agent_write_pattern(host_ref: str) -> str:
    """RabbitMQ write regex for a host's agent user: its own queues PLUS the
    default exchange (`amq.default` / `""`). The agent publishes every message
    (response, agent_status, *_inventory, *_metrics) through the default exchange
    with the queue name as the routing key, so write on `amq.default` is required
    or the consumer fails to start with ACCESS_REFUSED."""
    return f"^({host_ref}\\.{_QUEUE_BODY}|amq\\.default|)$"
