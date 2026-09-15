"""Add durable synchronization state for the trusted Repo Manager.

Revision ID: 20260909_0006
Revises: 20260908_0005
"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260909_0006"
down_revision: Union[str, Sequence[str], None] = "20260908_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch_op:
        batch_op.add_column(
            sa.Column(
                "sync_generation",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "sync_failure_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("sync_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sync_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sync_finished_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sync_next_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sync_lease_owner", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sync_lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_sync_error_code", sa.String(length=80), nullable=True)
        )

    repositories = sa.table(
        "repositories",
        sa.column("enabled", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("sync_requested_at", sa.DateTime(timezone=True)),
        sa.column("sync_next_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.get_bind().execute(
        repositories.update().values(
            status="pending_validation",
            sync_requested_at=now,
            sync_next_at=sa.case((repositories.c.enabled.is_(True), now), else_=None),
        )
    )

    with op.batch_alter_table("repositories") as batch_op:
        batch_op.drop_constraint("ck_repositories_status", type_="check")
        batch_op.create_check_constraint(
            "ck_repositories_status",
            "status IN ('pending_validation', 'validating', 'ready', 'unavailable', 'invalid')",
        )
        batch_op.create_check_constraint(
            "ck_repositories_sync_generation",
            "sync_generation >= 0",
        )
        batch_op.create_check_constraint(
            "ck_repositories_sync_failure_count",
            "sync_failure_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_repositories_sync_lease_pair",
            "((sync_lease_owner IS NULL AND sync_lease_expires_at IS NULL) "
            "OR (sync_lease_owner IS NOT NULL AND sync_lease_expires_at IS NOT NULL))",
        )
        batch_op.create_check_constraint(
            "ck_repositories_ready_state",
            "(status <> 'ready' OR (enabled IS TRUE "
            "AND default_branch IS NOT NULL "
            "AND length(default_branch) BETWEEN 1 AND 255 "
            "AND last_known_commit IS NOT NULL "
            "AND length(last_known_commit) IN (40, 64) "
            "AND last_fetched_at IS NOT NULL AND sync_finished_at IS NOT NULL "
            "AND sync_next_at IS NOT NULL AND sync_lease_owner IS NULL "
            "AND sync_lease_expires_at IS NULL AND last_sync_error_code IS NULL "
            "AND sync_failure_count = 0))",
        )
        batch_op.create_check_constraint(
            "ck_repositories_validating_state",
            "(status <> 'validating' OR (enabled IS TRUE "
            "AND sync_started_at IS NOT NULL AND sync_lease_owner IS NOT NULL "
            "AND sync_lease_expires_at IS NOT NULL))",
        )

    op.create_index(
        "ix_repositories_sync_queue",
        "repositories",
        ["enabled", "sync_next_at"],
        unique=False,
    )
    op.create_index(
        "ix_repositories_sync_lease",
        "repositories",
        ["sync_lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    # A validating row is not representable in G2.1. It returns to the safe
    # pending state; operational data remains available in the old columns.
    repositories = sa.table(
        "repositories",
        sa.column("status", sa.String()),
    )
    op.get_bind().execute(
        repositories.update()
        .where(repositories.c.status == "validating")
        .values(status="pending_validation")
    )

    op.drop_index("ix_repositories_sync_lease", table_name="repositories")
    op.drop_index("ix_repositories_sync_queue", table_name="repositories")
    with op.batch_alter_table("repositories") as batch_op:
        batch_op.drop_constraint("ck_repositories_validating_state", type_="check")
        batch_op.drop_constraint("ck_repositories_ready_state", type_="check")
        batch_op.drop_constraint("ck_repositories_sync_lease_pair", type_="check")
        batch_op.drop_constraint("ck_repositories_sync_failure_count", type_="check")
        batch_op.drop_constraint("ck_repositories_sync_generation", type_="check")
        batch_op.drop_constraint("ck_repositories_status", type_="check")
        batch_op.create_check_constraint(
            "ck_repositories_status",
            "status IN ('pending_validation', 'ready', 'unavailable', 'invalid')",
        )
        batch_op.drop_column("last_sync_error_code")
        batch_op.drop_column("sync_lease_expires_at")
        batch_op.drop_column("sync_lease_owner")
        batch_op.drop_column("sync_next_at")
        batch_op.drop_column("sync_finished_at")
        batch_op.drop_column("sync_started_at")
        batch_op.drop_column("sync_requested_at")
        batch_op.drop_column("sync_failure_count")
        batch_op.drop_column("sync_generation")
