from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

import aio_pika
from sqlalchemy import delete, select, update

from ..cache import release_vm_lock
from ..config import get_settings
from ..db import session_scope
from ..logging import configure_logging
from ..messaging import (
    CONSUMED_KINDS,
    QueueKind,
    close_connection,
    declare_host_queues,
    get_connection,
    queue_name,
)
from ..models import Host, HostMetric, Task, Vm, VmMetric
from ..models.task import TERMINAL_TASK_STATUSES
from ..task_timeouts import task_timeout_seconds
from .consumers import handle_message

log = logging.getLogger("ovc.worker")
_settings = get_settings()

HOST_SCAN_INTERVAL = 15
TIMEOUT_SWEEP_INTERVAL = 30
METRICS_SWEEP_INTERVAL = 300

# How long RabbitMQ may stay disconnected before giving up and exiting, so the
# orchestrator restarts the process fresh. connect_robust retries the
# underlying TCP connection forever, but per-host channels/consumers are not
# guaranteed to come back cleanly from every kind of broker event (a forced
# close during a cluster failover, e.g.) - staying alive in that state just
# means silently missing every host's messages.
RABBITMQ_MAX_DOWNTIME_SECONDS = 120

# Zombie detector: if hosts are registered but nothing at all has been consumed
# for this long, the consumers most likely died silently - exit for a restart.
CONSUMER_ZOMBIE_STARTUP_GRACE = 300
CONSUMER_ZOMBIE_THRESHOLD = 600

# Per-host stuck-consumer detector: agents report far more often than this, so
# silence past the threshold plus a non-empty response queue means our
# consumer died, not that the host went offline.
HOST_CONSUMER_SILENCE_THRESHOLD = 300

# Last time ANY message was processed, and per-host, for the watchdogs below.
_last_message_processed_at: float = time.monotonic()
_host_last_message_at: dict[str, float] = {}


async def _known_hosts() -> list[tuple[str, str]]:
    """(host UUID, host short_id). Queues are named by short_id; the inventory
    handlers key on the UUID PK."""
    async with session_scope() as db:
        return [
            (row.id, row.short_id)
            for row in (await db.execute(select(Host.id, Host.short_id))).all()
        ]


def _make_handler(host_id: str, short_id: str, kind: QueueKind):
    async def _on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        global _last_message_processed_at
        async with message.process(requeue=False):
            now = time.monotonic()
            _last_message_processed_at = now
            _host_last_message_at[short_id] = now
            try:
                await handle_message(host_id, kind, message.body)
            except Exception:  # noqa: BLE001 - never let one bad message kill the consumer
                log.exception("error handling %s.%s", host_id, kind.value)

    return _on_message


async def _subscribe_host(
    connection: aio_pika.abc.AbstractRobustConnection, host_id: str, short_id: str
) -> aio_pika.abc.AbstractChannel | None:
    """Subscribe to every queue of one host, on a channel of its own.

    Every host gets its OWN channel, on purpose. A declare the broker rejects
    (PRECONDITION_FAILED on an argument mismatch, 404 on a bad passive declare)
    is a channel-level exception: it kills the channel it ran on, along with
    every consumer already registered there. On a single shared channel this
    takes down every other host's subscriptions from the same pass - the
    worker then looks perfectly healthy while several hosts are never consumed
    again.

    Returns the channel on success, or None when the host could not be
    subscribed, so the caller retries it on the next pass instead of one bad
    host blocking discovery for everybody else.
    """
    channel = await connection.channel()
    try:
        await channel.set_qos(prefetch_count=16)
        queues = await declare_host_queues(channel, short_id)
        for kind in CONSUMED_KINDS:
            await queues[kind].consume(_make_handler(host_id, short_id, kind))
        log.info(
            "subscribed to host=%s (%s)", host_id, queue_name(short_id, QueueKind.RESPONSE)
        )
    except Exception:
        log.exception("failed to subscribe host=%s short_id=%s", host_id, short_id)
        try:
            await channel.close()
        except Exception:  # noqa: BLE001
            pass
        return None

    _host_last_message_at[short_id] = time.monotonic()
    return channel


async def _timeout_sweeper() -> None:
    while True:
        await asyncio.sleep(TIMEOUT_SWEEP_INTERVAL)
        try:
            await _sweep_stale_tasks()
        except Exception:  # noqa: BLE001 - a transient DB/cache error must not kill the sweeper
            log.exception("timeout sweep failed; will retry")


async def _sweep_stale_tasks() -> None:
    now = datetime.now(UTC)
    # SQL narrows to anything past the *shortest* deadline; the per-kind check
    # below keeps long-running kinds (e.g. vm_shutdown) that still have time.
    default_cutoff = now - timedelta(seconds=_settings.task_timeout_seconds)
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(
                    Task.id,
                    Task.kind,
                    Task.target_type,
                    Task.target_id,
                    Task.created_at,
                )
                .where(Task.status.notin_(TERMINAL_TASK_STATUSES))
                .where(Task.created_at < default_cutoff)
            )
        ).all()
        stale = [
            t
            for t in rows
            if t.created_at
            < now - timedelta(seconds=task_timeout_seconds(t.kind))
        ]
        if stale:
            await db.execute(
                update(Task)
                .where(Task.id.in_([t.id for t in stale]))
                .values(
                    status="timeout",
                    finished_at=datetime.now(UTC),
                    error="Agent did not respond in time",
                )
            )
            log.warning("timed out %d stale task(s)", len(stale))
            # free any VM these tasks were holding
            for t in stale:
                if t.target_type == "vm":
                    await release_vm_lock(t.target_id, t.id)

        # A vm_create / vm_clone whose agent never answered leaves an unusable
        # placeholder Vm (no vm_uuid) that the UI cannot delete - drop it.
        orphans = await db.execute(
            delete(Vm).where(
                Vm.vm_uuid.is_(None),
                Vm.id.in_(
                    select(Task.target_id).where(
                        Task.kind.in_(("vm_create", "vm_clone")),
                        Task.status.in_(("timeout", "failed")),
                    )
                ),
            )
        )
        if orphans.rowcount:
            log.warning(
                "removed %d orphaned vm_create/vm_clone placeholder(s)",
                orphans.rowcount,
            )


async def _metrics_retention_sweeper() -> None:
    """Safety net for the prune-on-write in services.inventory: drop quick-metrics
    samples from hosts that stopped reporting."""
    while True:
        await asyncio.sleep(METRICS_SWEEP_INTERVAL)
        cutoff = datetime.now(UTC) - timedelta(
            seconds=_settings.metrics_retention_seconds
        )
        async with session_scope() as db:
            for model in (HostMetric, VmMetric):
                result = await db.execute(
                    delete(model).where(model.ts < cutoff)
                )
                if result.rowcount:
                    log.info(
                        "pruned %d stale %s row(s)", result.rowcount, model.__tablename__
                    )


async def run() -> None:
    configure_logging()
    log.info("starting ovc-backend worker")

    # --- RabbitMQ connection with close/reconnect tracking ---
    # connect_robust auto-reconnects the underlying TCP connection, but that is
    # not the same as every per-host channel/consumer coming back: a broker
    # event that closes a channel without dropping the connection (e.g. a
    # PRECONDITION_FAILED on some other host) never triggers robust recovery at
    # all. These callbacks + the watchdogs below are the safety net that
    # detects "quietly stuck" instead of staying deaf forever.
    rabbitmq_healthy = True
    rabbitmq_lost_at: float | None = None

    def _on_connection_close(
        sender: aio_pika.abc.AbstractConnection, exc: BaseException | None
    ) -> None:
        nonlocal rabbitmq_healthy, rabbitmq_lost_at
        rabbitmq_healthy = False
        if rabbitmq_lost_at is None:
            rabbitmq_lost_at = time.monotonic()
        log.warning("rabbitmq_connection_closed error=%s", exc or "clean close")

    def _on_connection_reconnect(sender: aio_pika.abc.AbstractConnection) -> None:
        nonlocal rabbitmq_healthy, rabbitmq_lost_at
        rabbitmq_healthy = True
        rabbitmq_lost_at = None
        log.info("rabbitmq_connection_reconnected")

    connection = await get_connection()
    connection.close_callbacks.add(_on_connection_close)
    connection.reconnect_callbacks.add(_on_connection_reconnect)

    # host short_id -> the channel it owns.
    host_channels: dict[str, aio_pika.abc.AbstractChannel] = {}

    async def unsubscribe_host(short_id: str) -> None:
        """Forget a host so the next discovery pass subscribes it from scratch."""
        _host_last_message_at.pop(short_id, None)
        channel = host_channels.pop(short_id, None)
        if channel is not None:
            try:
                await channel.close()
            except Exception:  # noqa: BLE001
                pass

    async def refresh_subscriptions() -> None:
        """Subscribe to the queues of every host we are not consuming yet.

        A host is recorded as subscribed only once its subscription actually
        succeeded, and one failing host never blocks the rest of the pass -
        neither held before: a single shared channel plus no per-host
        try/except meant one bad host's exception aborted discovery for
        everybody else in the same pass, and the DB query order meant it kept
        being tried first on every retry, blocking new hosts indefinitely.
        """
        hosts = await _known_hosts()

        known_short_ids = {short_id for _, short_id in hosts}
        for gone in list(host_channels.keys() - known_short_ids):
            log.info("host_removed_unsubscribing short_id=%s", gone)
            await unsubscribe_host(gone)

        pending = [
            (host_id, short_id) for host_id, short_id in hosts if short_id not in host_channels
        ]
        subscribed = 0
        for host_id, short_id in pending:
            channel = await _subscribe_host(connection, host_id, short_id)
            if channel is None:
                continue
            host_channels[short_id] = channel
            subscribed += 1

        if pending:
            log.info(
                "host_subscription_pass candidates=%d subscribed=%d failed=%d total_monitored=%d",
                len(pending),
                subscribed,
                len(pending) - subscribed,
                len(host_channels),
            )

    # tolerate the API still running migrations on a cold start
    for attempt in range(30):
        try:
            await refresh_subscriptions()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("waiting for schema (%s): %s", attempt, exc)
            await asyncio.sleep(2)

    log.info("task_consumer_ready hosts_monitored=%d", len(host_channels))

    async def check_rabbitmq_health() -> None:
        """connect_robust keeps retrying the connection forever; if it has not
        come back for too long, exit so the orchestrator restarts us fresh
        instead of sitting there disconnected."""
        while True:
            await asyncio.sleep(15)
            if not rabbitmq_healthy and rabbitmq_lost_at is not None:
                elapsed = time.monotonic() - rabbitmq_lost_at
                log.warning("rabbitmq_still_disconnected elapsed_seconds=%d", int(elapsed))
                if elapsed >= RABBITMQ_MAX_DOWNTIME_SECONDS:
                    log.critical(
                        "rabbitmq_connection_lost_fatal elapsed_seconds=%d - exiting for restart",
                        int(elapsed),
                    )
                    sys.exit(1)

    async def check_consumer_health() -> None:
        """Zombie detector: the connection looks fine but nothing at all has
        been consumed in a long time even though hosts are registered - the
        consumers most likely died silently during a reconnect. Exit for a
        clean restart."""
        await asyncio.sleep(CONSUMER_ZOMBIE_STARTUP_GRACE)
        while True:
            await asyncio.sleep(60)
            if not host_channels:
                continue
            elapsed = time.monotonic() - _last_message_processed_at
            if elapsed >= CONSUMER_ZOMBIE_THRESHOLD:
                log.critical(
                    "consumer_zombie_detected seconds_since_last_message=%d "
                    "monitored_hosts=%d - exiting",
                    int(elapsed),
                    len(host_channels),
                )
                sys.exit(1)

    async def check_host_consumers() -> None:
        """Detect and repair a PARTIAL consumer outage, per host.

        check_consumer_health only fires when nothing at all arrives, so it is
        blind to the more common case: most hosts keep being consumed (and
        keep the global timer fresh) while a few lost their consumer entirely.
        A silent host alone is not proof of a problem - it may simply be
        offline - so the response queue's backlog is what tells them apart: a
        queue with a live consumer and prefetch 16 does not sit non-empty for
        minutes.
        """
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for short_id, channel in list(host_channels.items()):
                silent_for = now - _host_last_message_at.get(short_id, now)
                if silent_for < HOST_CONSUMER_SILENCE_THRESHOLD:
                    continue

                channel_dead = channel.is_closed
                backlog: int | None = None
                if not channel_dead:
                    try:
                        probe = await channel.declare_queue(
                            queue_name(short_id, QueueKind.RESPONSE), durable=True
                        )
                        backlog = probe.declaration_result.message_count
                    except Exception as exc:  # noqa: BLE001
                        channel_dead = True
                        log.warning(
                            "host_queue_probe_failed short_id=%s error=%s", short_id, exc
                        )

                if channel_dead or backlog:
                    log.warning(
                        "host_consumer_stuck_resubscribing short_id=%s silent_seconds=%d "
                        "response_backlog=%s channel_dead=%s",
                        short_id,
                        int(silent_for),
                        backlog,
                        channel_dead,
                    )
                    # Dropped here, re-subscribed by the next discovery pass.
                    await unsubscribe_host(short_id)

    stale_checker_task = asyncio.create_task(_timeout_sweeper())
    metrics_sweeper_task = asyncio.create_task(_metrics_retention_sweeper())
    rabbitmq_health_task = asyncio.create_task(check_rabbitmq_health())
    consumer_health_task = asyncio.create_task(check_consumer_health())
    host_consumer_task = asyncio.create_task(check_host_consumers())

    try:
        while True:
            await asyncio.sleep(HOST_SCAN_INTERVAL)
            try:
                await refresh_subscriptions()
            except Exception:  # noqa: BLE001
                log.exception("subscription refresh failed; will retry")
    finally:
        stale_checker_task.cancel()
        metrics_sweeper_task.cancel()
        rabbitmq_health_task.cancel()
        consumer_health_task.cancel()
        host_consumer_task.cancel()
        await close_connection()


if __name__ == "__main__":
    asyncio.run(run())
