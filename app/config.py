from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OVC_", extra="ignore")

    # --- app ---
    env: Literal["dev", "prod", "test"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False
    api_prefix: str = "/api"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- database ---
    # async DSN. Defaults match ../infra-containers (postgres on localhost, ovc/ovc123).
    database_url: str = "postgresql+asyncpg://ovc:ovc123@localhost:5432/ovc"
    db_echo: bool = False

    # --- cache (Valkey / Redis wire compatible) ---
    valkey_url: str = "redis://localhost:6379/0"
    vm_state_ttl_seconds: int = 3600

    # --- RabbitMQ ---
    rabbitmq_url: str = "amqp://ovc:ovc123@localhost:5672/"
    # HTTP management API (rabbitmq:*-management), used by the startup preflight to
    # inspect/create queues, per-host agent users and their permissions.
    rabbitmq_mgmt_url: str = "http://localhost:15672"
    # AMQP URL an agent on a Hyper-V host uses to reach the broker, WITHOUT
    # credentials (e.g. "amqp://rabbit.corp.local:5672/"). The per-host agent
    # user/password are injected when rendering its config.ini. Empty ⇒ reuse
    # rabbitmq_url's scheme/host/port/vhost (fine when the agent shares the dev network).
    agent_rabbitmq_url: str = ""
    # last-message-only queues keep just the newest payload
    inventory_queue_max_length: int = 1
    # an agent is considered offline if agent_status is older than this
    agent_offline_after_seconds: int = 120
    # quick host/VM metrics: how long a sample is kept before the worker prunes it
    metrics_retention_seconds: int = 3600

    # --- startup preflight ---
    # Verify (and repair) Valkey / Postgres / RabbitMQ on boot; the process logs
    # the failure and exits if a check can't be satisfied. See app/preflight.py.
    preflight: bool = True
    # run `alembic upgrade head` from the preflight instead of the container command
    preflight_run_migrations: bool = True
    # create/refresh a per-host RabbitMQ user via the management API
    preflight_manage_rabbitmq_users: bool = True

    # --- auth ---
    # "stub" trusts every request as a fixed dev principal; "oidc" verifies JWTs
    # from any OpenID Connect provider (Keycloak, Auth0, Okta, Entra ID, …).
    auth_mode: Literal["stub", "oidc"] = "stub"
    stub_user_email: str = "operator@ovc.local"
    # ../infra-containers ships a Keycloak realm at http://localhost:8080 (admin/admin)
    oidc_issuer: str = ""
    oidc_audience: str = "ovc-frontend"
    oidc_jwks_url: str = ""
    # OIDC client id - substituted for "${client_id}" in oidc_roles_claim
    oidc_client_id: str = "ovc-frontend"
    # dot-path to the roles array in the token ("${client_id}" is substituted):
    #   Keycloak client roles (default): resource_access.${client_id}.roles
    #   Keycloak realm roles           : realm_access.roles
    #   Auth0 / Okta / flat claim      : roles  (or "https://ovc.example/roles")
    oidc_roles_claim: str = "resource_access.${client_id}.roles"
    # this role bypasses every scope filter (full access, no ScopeGrant needed)
    admin_role: str = "ADMINISTRATOR"
    # seconds a fetched JWKS document is cached before re-download
    oidc_jwks_ttl_seconds: int = 3600
    session_secret: str = "change-me-before-prod"

    # --- agent binary storage ---
    # Where uploaded ovc-agent binaries live. "local" = a directory mounted into
    # the API/worker containers (a k8s PVC, a docker volume); "s3" = any
    # S3-compatible object store. Switching backends needs a redeploy - the
    # Agent Management screen only *displays* the active one.
    agent_storage: Literal["local", "s3"] = "local"
    # absolute → used as-is (mount a volume there); relative → resolved against
    # the app root (so "uploads/…" is <repo>/uploads/… locally and /app/uploads/…
    # in the container). See app/services/agent_store.py.
    agent_storage_dir: str = "uploads/agent-binaries"
    agent_s3_bucket: str = ""
    agent_s3_prefix: str = "agent/"
    # non-empty for MinIO / Ceph / other non-AWS endpoints (e.g. http://minio:9000)
    agent_s3_endpoint_url: str = ""
    agent_s3_region: str = "us-east-1"
    # S3 credentials come from the standard AWS_* env / instance role (botocore
    # default chain) - they are NOT OVC_-prefixed.
    #
    # How long a download URL handed to an agent stays valid: the S3 presign
    # lifetime, or the HMAC-token lifetime for local-mode downloads.
    agent_download_url_ttl_seconds: int = 900
    # How long the copy-paste "install.ps1" URL handed to an operator stays valid
    # (HMAC-token lifetime). Longer than a download URL - someone copies it, then
    # walks over to the Hyper-V host to paste it.
    agent_install_url_ttl_seconds: int = 3600
    # External base URL a Hyper-V *host* uses to reach this API (e.g.
    # "https://ovc.corp.local"). Required for local-mode agent upgrades - the
    # agent downloads the binary over plain HTTP from
    # {public_base_url}{api_prefix}/agent-binaries/{id}/download. Empty ⇒
    # local-mode downloads are disabled and rollout fails fast with a clear error.
    public_base_url: str = ""

    # --- task lifecycle ---
    task_timeout_seconds: int = 300

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
