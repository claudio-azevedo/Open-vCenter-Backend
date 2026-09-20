"""Per-kind task deadlines.

The default (``OVC_TASK_TIMEOUT_SECONDS``) suits quick agent operations. A few
kinds legitimately run for minutes - a graceful guest shutdown, a storage
migration, disk creation. Each entry here is kept comfortably above the agent's
own hard kill (``ovc-agent-hyperv`` ``internal/jobqueue/monitor.go``) so the
backend never times a task out - or lets its VM lock expire - while the agent is
still working on it.

Used by:
  * ``app.worker.main._sweep_stale_tasks`` - when to flip a task to ``timeout``
  * ``app.cache`` - the VM-lock TTL (crash backstop)
"""

from __future__ import annotations

from .config import get_settings

_LONG_TASK_TIMEOUTS: dict[str, int] = {
    "vm_shutdown": 900,
    "vm_create": 2100,
    "vm_move": 2100,
    "vm_rename": 2100,
    "vm_export_template": 2100,
    "vm_batch_start": 2100,
    "vm_batch_stop": 2100,
    "vm_clone": 2100,
    "vm_edit": 2100,
    # download of a ~12-40 MiB binary + checksum + drain + service swap + the
    # NEW agent starting and publishing the terminal response. The old agent
    # goes quiet at 99% during the swap, so keep the window generous.
    "host_update_agent": 1800,
}


def task_timeout_seconds(kind: str | None) -> int:
    """The deadline for a task of this ``kind`` - the configured default, or a
    longer per-kind override when one applies."""
    default = get_settings().task_timeout_seconds
    return max(_LONG_TASK_TIMEOUTS.get(kind or "", 0), default)


def max_task_timeout_seconds() -> int:
    """The longest deadline any kind can have - the SQL pre-filter bound."""
    return max(get_settings().task_timeout_seconds, *_LONG_TASK_TIMEOUTS.values())
