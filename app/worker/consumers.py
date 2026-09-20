from __future__ import annotations

import json
import logging

from ..db import session_scope
from ..messaging import AgentResponse, QueueKind
from ..services import inventory

log = logging.getLogger("ovc.worker")


async def handle_message(host_id: str, kind: QueueKind, body: bytes) -> None:
    try:
        payload = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError):
        log.warning("dropping non-JSON message on %s.%s", host_id, kind.value)
        return

    async with session_scope() as db:
        if kind is QueueKind.RESPONSE:
            await inventory.apply_response(
                db, host_id, AgentResponse.model_validate(payload)
            )
        elif kind is QueueKind.AGENT_STATUS:
            await inventory.apply_agent_status(db, host_id, payload)
        elif kind is QueueKind.VM_INVENTORY:
            await inventory.apply_vm_inventory(db, host_id, payload)
        elif kind is QueueKind.HOST_INVENTORY:
            await inventory.apply_host_inventory(db, host_id, payload)
        elif kind is QueueKind.TEMPLATE_INVENTORY:
            await inventory.apply_template_inventory(db, host_id, payload)
        elif kind is QueueKind.ISO_INVENTORY:
            await inventory.apply_iso_inventory(db, host_id, payload)
        elif kind is QueueKind.HOST_METRICS:
            await inventory.apply_host_metrics(db, host_id, payload)
        elif kind is QueueKind.VM_METRICS:
            await inventory.apply_vm_metrics(db, host_id, payload)
        else:  # pragma: no cover
            log.warning("no handler for queue kind %s", kind)
