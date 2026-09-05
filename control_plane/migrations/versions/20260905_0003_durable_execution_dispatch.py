"""Allow durable execution rows before OpenCode session creation.

Revision ID: 20260905_0003
Revises: 20260905_0002
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0003"
down_revision: Union[str, Sequence[str], None] = "20260905_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table keeps the migration executable in the SQLite migration
    # safety suite while producing a normal ALTER COLUMN on PostgreSQL.
    with op.batch_alter_table("execution_runs") as batch_op:
        batch_op.alter_column(
            "opencode_session_id",
            existing_type=sa.String(length=120),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_runs") as batch_op:
        batch_op.alter_column(
            "opencode_session_id",
            existing_type=sa.String(length=120),
            nullable=False,
        )
