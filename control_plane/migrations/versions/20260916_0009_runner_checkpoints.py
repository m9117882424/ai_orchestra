"""Bind runner jobs to durable LLM checkpoints and exact source snapshots.

Revision ID: 20260916_0009
Revises: 20260916_0008
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0009"
down_revision: Union[str, Sequence[str], None] = "20260916_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("runner_jobs") as batch:
        batch.add_column(sa.Column("source_snapshot_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("checkpoint_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("checkpoint_command_index", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("checkpoint_label", sa.String(length=80), nullable=True))
        batch.create_check_constraint(
            "ck_runner_jobs_source_snapshot_digest",
            "source_snapshot_digest IS NULL OR length(source_snapshot_digest) = 64",
        )
        batch.create_check_constraint(
            "ck_runner_jobs_checkpoint_digest",
            "checkpoint_digest IS NULL OR length(checkpoint_digest) = 64",
        )
        batch.create_check_constraint(
            "ck_runner_jobs_checkpoint_binding",
            "((checkpoint_digest IS NULL AND checkpoint_command_index IS NULL "
            "AND checkpoint_label IS NULL) OR "
            "(checkpoint_digest IS NOT NULL AND checkpoint_command_index IS NOT NULL "
            "AND checkpoint_command_index >= 0 AND source_snapshot_digest IS NOT NULL))",
        )
        batch.create_index(
            "ux_runner_jobs_checkpoint_command",
            ["execution_id", "checkpoint_digest", "checkpoint_command_index"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("runner_jobs") as batch:
        batch.drop_index("ux_runner_jobs_checkpoint_command")
        batch.drop_constraint("ck_runner_jobs_checkpoint_binding", type_="check")
        batch.drop_constraint("ck_runner_jobs_checkpoint_digest", type_="check")
        batch.drop_constraint("ck_runner_jobs_source_snapshot_digest", type_="check")
        batch.drop_column("checkpoint_label")
        batch.drop_column("checkpoint_command_index")
        batch.drop_column("checkpoint_digest")
        batch.drop_column("source_snapshot_digest")
