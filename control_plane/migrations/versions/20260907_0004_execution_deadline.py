"""Add a durable execution deadline.

Revision ID: 20260907_0004
Revises: 20260905_0003
"""
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260907_0004"
down_revision: Union[str, Sequence[str], None] = "20260905_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_runs",
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_execution_runs_active_deadline",
        "execution_runs",
        ["status", "deadline_at"],
        unique=False,
    )

    # Existing active executions receive a full grace period at migration time.
    # Terminal history remains nullable because no timeout decision is pending.
    execution_runs = sa.table(
        "execution_runs",
        sa.column("status", sa.String()),
        sa.column("deadline_at", sa.DateTime(timezone=True)),
    )
    grace_deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    op.get_bind().execute(
        execution_runs.update()
        .where(execution_runs.c.status.in_(("queued", "running")))
        .values(deadline_at=grace_deadline)
    )


def downgrade() -> None:
    op.drop_index("ix_execution_runs_active_deadline", table_name="execution_runs")
    op.drop_column("execution_runs", "cancel_requested_at")
    op.drop_column("execution_runs", "deadline_at")
