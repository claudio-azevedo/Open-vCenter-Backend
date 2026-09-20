from __future__ import annotations

import secrets
import uuid


def new_id() -> str:
    """A fresh UUIDv4 string - the id for every backend-generated entity."""
    return str(uuid.uuid4())


def new_host_short_id() -> str:
    """A short, opaque host handle used everywhere the full UUID is too noisy:
    RabbitMQ queue names (`<short_id>.request`), the per-host agent username,
    the agent `config.ini`, worker log lines. 10 hex chars - matches the
    `<hostid>` shape in docs/agent-queue-contract.md, collision-safe for the
    host counts this manages, a third the length of a UUID."""
    return uuid.uuid4().hex[:10]


def new_agent_rmq_user(host_ref: str) -> str:
    """`host_ref` is the host `short_id` (not the UUID PK)."""
    return f"agent-{host_ref}"


def new_agent_rmq_password() -> str:
    """URL-safe secret for a host agent's RabbitMQ login."""
    return secrets.token_urlsafe(24)
