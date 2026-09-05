"""Add durable execution worker lease and fencing state.

Revision ID: 20260905_0002
Revises: 20260904_0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0002"
down_revision: Union[str, Sequence[str], None] = "20260904_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_runs",
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "execution_runs",
        sa.Column(
            "lease_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "execution_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_execution_runs_lease_expires_at"),
        "execution_runs",
        ["lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_runs_lease_expires_at"), table_name="execution_runs")
    op.drop_column("execution_runs", "lease_expires_at")
    op.drop_column("execution_runs", "heartbeat_at")
    op.drop_column("execution_runs", "lease_generation")
    op.drop_column("execution_runs", "lease_owner")
