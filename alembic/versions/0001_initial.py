"""initial schema (consolidated)

Squash of the historical 0001..0010 migrations into a single baseline for a
fresh database. Structurally equivalent to running all of them in order.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clusters",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("hypervisor", sa.String(length=32), server_default="hyperv", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clusters")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("roles", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "hosts",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("short_id", sa.String(length=16), nullable=False),
        sa.Column("cluster_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("fqdn", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("hypervisor", sa.String(length=32), server_default="hyperv", nullable=False),
        sa.Column("agent_rmq_user", sa.String(length=64), nullable=True),
        sa.Column("agent_rmq_password", sa.String(length=128), nullable=True),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("agent_last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_refresh_vm", sa.Integer(), nullable=True),
        sa.Column("agent_refresh_host", sa.Integer(), nullable=True),
        sa.Column("hardware", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["clusters.id"],
            name=op.f("fk_hosts_cluster_id_clusters"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hosts")),
        sa.UniqueConstraint("short_id", name=op.f("uq_hosts_short_id")),
    )
    op.create_index(op.f("ix_hosts_cluster_id"), "hosts", ["cluster_id"], unique=False)
    op.create_table(
        "scope_grants",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_scope_grants_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scope_grants")),
        sa.UniqueConstraint("user_id", "level", "scope_id", name="user_level_scope"),
    )
    op.create_index(op.f("ix_scope_grants_user_id"), "scope_grants", ["user_id"], unique=False)
    op.create_table(
        "folders",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("cluster_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("host_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(host_id IS NOT NULL AND cluster_id IS NULL) "
            "OR (host_id IS NULL AND cluster_id IS NOT NULL)",
            name=op.f("ck_folders_folder_scope_exactly_one"),
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["clusters.id"],
            name=op.f("fk_folders_cluster_id_clusters"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["host_id"], ["hosts.id"], name=op.f("fk_folders_host_id_hosts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_folders")),
    )
    op.create_index(op.f("ix_folders_cluster_id"), "folders", ["cluster_id"], unique=False)
    op.create_index(op.f("ix_folders_host_id"), "folders", ["host_id"], unique=False)
    op.create_table(
        "host_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("host_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("cpu_percent", sa.Float(), nullable=True),
        sa.Column("mem_percent", sa.Float(), nullable=True),
        sa.Column("disk_latency_ms", sa.Float(), nullable=True),
        sa.Column("net_rx_bps", sa.BigInteger(), nullable=True),
        sa.Column("net_tx_bps", sa.BigInteger(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_host_metrics_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_host_metrics")),
    )
    op.create_index(op.f("ix_host_metrics_host_id"), "host_metrics", ["host_id"], unique=False)
    op.create_index(op.f("ix_host_metrics_ts"), "host_metrics", ["ts"], unique=False)
    op.create_table(
        "isos",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("host_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["host_id"], ["hosts.id"], name=op.f("fk_isos_host_id_hosts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_isos")),
    )
    op.create_index(op.f("ix_isos_host_id"), "isos", ["host_id"], unique=False)
    op.create_index(op.f("ix_isos_checksum"), "isos", ["checksum"], unique=False)
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("target_name", sa.String(length=255), nullable=True),
        sa.Column("host_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("progress_message", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["host_id"], ["hosts.id"], name=op.f("fk_tasks_host_id_hosts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_index(op.f("ix_tasks_correlation_id"), "tasks", ["correlation_id"], unique=False)
    op.create_index(op.f("ix_tasks_host_id"), "tasks", ["host_id"], unique=False)
    op.create_index(op.f("ix_tasks_target_id"), "tasks", ["target_id"], unique=False)
    op.create_table(
        "templates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("host_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("disk_size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cpu_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("memory_mb", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("guest_os", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("image_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["host_id"], ["hosts.id"], name=op.f("fk_templates_host_id_hosts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_templates")),
    )
    op.create_index(op.f("ix_templates_host_id"), "templates", ["host_id"], unique=False)
    op.create_table(
        "agent_binaries",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column(
            "hypervisor",
            sa.String(length=32),
            server_default="hyperv",
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("uploaded_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_binaries")),
        sa.UniqueConstraint(
            "version", "hypervisor", name="uq_agent_binaries_version_hypervisor"
        ),
    )
    # at most one active build per hypervisor
    op.create_index(
        "uq_agent_binaries_active_per_hypervisor",
        "agent_binaries",
        ["hypervisor"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "vlans",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("cluster_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("host_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(host_id IS NOT NULL AND cluster_id IS NULL) "
            "OR (host_id IS NULL AND cluster_id IS NOT NULL)",
            name=op.f("ck_vlans_vlan_scope_exactly_one"),
        ),
        sa.CheckConstraint(
            "vlan_id BETWEEN 1 AND 4094",
            name=op.f("ck_vlans_vlan_tag_range"),
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["clusters.id"],
            name=op.f("fk_vlans_cluster_id_clusters"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_vlans_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vlans")),
    )
    op.create_index(op.f("ix_vlans_cluster_id"), "vlans", ["cluster_id"], unique=False)
    op.create_index(op.f("ix_vlans_host_id"), "vlans", ["host_id"], unique=False)
    op.create_index(
        "uq_vlans_cluster_tag",
        "vlans",
        ["cluster_id", "vlan_id"],
        unique=True,
        postgresql_where=sa.text("cluster_id IS NOT NULL"),
    )
    op.create_index(
        "uq_vlans_host_tag",
        "vlans",
        ["host_id", "vlan_id"],
        unique=True,
        postgresql_where=sa.text("host_id IS NOT NULL"),
    )
    op.create_table(
        "vms",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("vm_uuid", sa.String(length=64), nullable=True),
        sa.Column("host_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("cluster_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("folder_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("firmware", sa.String(length=8), server_default="UEFI", nullable=False),
        sa.Column("uptime_sec", sa.Integer(), nullable=True),
        sa.Column("vcpu", sa.Integer(), nullable=False),
        sa.Column("memory_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("memory_min_bytes", sa.BigInteger(), nullable=True),
        sa.Column("memory_max_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "memory_dynamic", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("memory_demand_bytes", sa.BigInteger(), nullable=True),
        sa.Column("cpu_usage_percent", sa.Float(), nullable=True),
        sa.Column("secure_boot", sa.Boolean(), nullable=True),
        sa.Column("secure_boot_template", sa.String(length=64), nullable=True),
        sa.Column(
            "nested_virtualization",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("auto_start_action", sa.String(length=32), nullable=True),
        sa.Column("auto_start_delay_sec", sa.Integer(), nullable=True),
        sa.Column("auto_stop_action", sa.String(length=32), nullable=True),
        sa.Column("config_path", sa.String(length=1024), nullable=True),
        sa.Column("dvd_path", sa.String(length=1024), nullable=True),
        sa.Column(
            "highly_available",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("metrics_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("vm_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inventory_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["clusters.id"],
            name=op.f("fk_vms_cluster_id_clusters"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["folders.id"],
            name=op.f("fk_vms_folder_id_folders"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["host_id"], ["hosts.id"], name=op.f("fk_vms_host_id_hosts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vms")),
    )
    op.create_index(op.f("ix_vms_cluster_id"), "vms", ["cluster_id"], unique=False)
    op.create_index(op.f("ix_vms_folder_id"), "vms", ["folder_id"], unique=False)
    op.create_index(op.f("ix_vms_host_id"), "vms", ["host_id"], unique=False)
    op.create_index(op.f("ix_vms_vm_uuid"), "vms", ["vm_uuid"], unique=False)
    # one row per Hyper-V VM GUID inside a cluster, or inside a standalone host
    op.create_index(
        "uq_vms_cluster_vm_uuid",
        "vms",
        ["cluster_id", "vm_uuid"],
        unique=True,
        postgresql_where=sa.text("cluster_id IS NOT NULL AND vm_uuid IS NOT NULL"),
    )
    op.create_index(
        "uq_vms_host_vm_uuid",
        "vms",
        ["host_id", "vm_uuid"],
        unique=True,
        postgresql_where=sa.text("cluster_id IS NULL AND vm_uuid IS NOT NULL"),
    )
    op.create_table(
        "vm_disks",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("vm_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("disk_id", sa.String(length=128), nullable=True),
        sa.Column("path", sa.String(length=1024), nullable=True),
        sa.Column("controller", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=True),
        sa.Column("format", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(
            ["vm_id"], ["vms.id"], name=op.f("fk_vm_disks_vm_id_vms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vm_disks")),
    )
    op.create_index(op.f("ix_vm_disks_vm_id"), "vm_disks", ["vm_id"], unique=False)
    op.create_table(
        "vm_nics",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("vm_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("nic_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("switch_name", sa.String(length=255), nullable=True),
        sa.Column("mac_address", sa.String(length=64), nullable=True),
        sa.Column("ip_addresses", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("connected", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["vm_id"], ["vms.id"], name=op.f("fk_vm_nics_vm_id_vms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vm_nics")),
    )
    op.create_index(op.f("ix_vm_nics_vm_id"), "vm_nics", ["vm_id"], unique=False)
    op.create_table(
        "vm_snapshots",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("vm_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("parent_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("snapshot_type", sa.String(length=32), nullable=True),
        sa.Column("snapshot_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["vm_id"],
            ["vms.id"],
            name=op.f("fk_vm_snapshots_vm_id_vms"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vm_snapshots")),
    )
    op.create_index(op.f("ix_vm_snapshots_vm_id"), "vm_snapshots", ["vm_id"], unique=False)
    op.create_table(
        "vm_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("vm_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("cpu_percent", sa.Float(), nullable=True),
        sa.Column("mem_bytes", sa.BigInteger(), nullable=True),
        sa.Column("disk_bytes", sa.BigInteger(), nullable=True),
        sa.Column("net_rx_bytes", sa.BigInteger(), nullable=True),
        sa.Column("net_tx_bytes", sa.BigInteger(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["vm_id"], ["vms.id"], name=op.f("fk_vm_metrics_vm_id_vms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vm_metrics")),
    )
    op.create_index(op.f("ix_vm_metrics_ts"), "vm_metrics", ["ts"], unique=False)
    op.create_index(op.f("ix_vm_metrics_vm_id"), "vm_metrics", ["vm_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_vm_metrics_vm_id"), table_name="vm_metrics")
    op.drop_index(op.f("ix_vm_metrics_ts"), table_name="vm_metrics")
    op.drop_table("vm_metrics")
    op.drop_index(op.f("ix_vm_snapshots_vm_id"), table_name="vm_snapshots")
    op.drop_table("vm_snapshots")
    op.drop_index(op.f("ix_vm_nics_vm_id"), table_name="vm_nics")
    op.drop_table("vm_nics")
    op.drop_index(op.f("ix_vm_disks_vm_id"), table_name="vm_disks")
    op.drop_table("vm_disks")
    op.drop_index("uq_vms_host_vm_uuid", table_name="vms")
    op.drop_index("uq_vms_cluster_vm_uuid", table_name="vms")
    op.drop_index(op.f("ix_vms_vm_uuid"), table_name="vms")
    op.drop_index(op.f("ix_vms_host_id"), table_name="vms")
    op.drop_index(op.f("ix_vms_folder_id"), table_name="vms")
    op.drop_index(op.f("ix_vms_cluster_id"), table_name="vms")
    op.drop_table("vms")
    op.drop_index("uq_vlans_host_tag", table_name="vlans")
    op.drop_index("uq_vlans_cluster_tag", table_name="vlans")
    op.drop_index(op.f("ix_vlans_host_id"), table_name="vlans")
    op.drop_index(op.f("ix_vlans_cluster_id"), table_name="vlans")
    op.drop_table("vlans")
    op.drop_index("uq_agent_binaries_active_per_hypervisor", table_name="agent_binaries")
    op.drop_table("agent_binaries")
    op.drop_index(op.f("ix_templates_host_id"), table_name="templates")
    op.drop_table("templates")
    op.drop_index(op.f("ix_tasks_target_id"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_host_id"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_correlation_id"), table_name="tasks")
    op.drop_table("tasks")
    op.drop_index(op.f("ix_isos_checksum"), table_name="isos")
    op.drop_index(op.f("ix_isos_host_id"), table_name="isos")
    op.drop_table("isos")
    op.drop_index(op.f("ix_host_metrics_ts"), table_name="host_metrics")
    op.drop_index(op.f("ix_host_metrics_host_id"), table_name="host_metrics")
    op.drop_table("host_metrics")
    op.drop_index(op.f("ix_folders_host_id"), table_name="folders")
    op.drop_index(op.f("ix_folders_cluster_id"), table_name="folders")
    op.drop_table("folders")
    op.drop_index(op.f("ix_scope_grants_user_id"), table_name="scope_grants")
    op.drop_table("scope_grants")
    op.drop_index(op.f("ix_hosts_cluster_id"), table_name="hosts")
    op.drop_table("hosts")
    op.drop_table("users")
    op.drop_table("clusters")
