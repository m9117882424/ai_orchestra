"""Add G4.2 child-run telemetry and automatic usage identity.

Revision ID: 20260917_0011
Revises: 20260917_0010
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260917_0011"
down_revision: Union[str, Sequence[str], None] = "20260917_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_child_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_run_id", sa.String(length=160), nullable=False),
        sa.Column("parent_source_run_id", sa.String(length=160), nullable=True),
        sa.Column("parent_call_id", sa.String(length=160), nullable=True),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("task_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("retry_of_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'unknown')",
            name="ck_execution_child_runs_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_execution_child_runs_attempt"),
        sa.CheckConstraint(
            "task_fingerprint IS NULL OR length(task_fingerprint) = 64",
            name="ck_execution_child_runs_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["execution_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_id"], ["execution_child_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_execution_child_runs_source",
        "execution_child_runs",
        ["execution_id", "source", "source_run_id"],
        unique=True,
    )
    op.create_index(
        "ix_execution_child_runs_timeline",
        "execution_child_runs",
        ["execution_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_child_runs_role",
        "execution_child_runs",
        ["execution_id", "role", "status"],
        unique=False,
    )

    with op.batch_alter_table("usage_events") as batch:
        batch.add_column(sa.Column("source", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("source_key", sa.String(length=160), nullable=True))
        batch.add_column(sa.Column("child_run_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_usage_events_child_run_id_execution_child_runs",
            "execution_child_runs",
            ["child_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_usage_events_child_run_id", ["child_run_id"], unique=False)
        batch.create_index(
            "ux_usage_events_source_key",
            ["execution_id", "source", "source_key"],
            unique=True,
        )
        batch.create_check_constraint(
            "ck_usage_events_source_pair",
            "((source IS NULL AND source_key IS NULL) OR "
            "(source IS NOT NULL AND source_key IS NOT NULL))",
        )

    with op.batch_alter_table("execution_result_packages") as batch:
        batch.drop_constraint("ck_execution_result_packages_version", type_="check")
        batch.create_check_constraint(
            "ck_execution_result_packages_version",
            "package_version IN (1, 2)",
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_result_packages") as batch:
        batch.drop_constraint("ck_execution_result_packages_version", type_="check")
        batch.create_check_constraint(
            "ck_execution_result_packages_version",
            "package_version = 1",
        )
    with op.batch_alter_table("usage_events") as batch:
        batch.drop_constraint("ck_usage_events_source_pair", type_="check")
        batch.drop_index("ux_usage_events_source_key")
        batch.drop_index("ix_usage_events_child_run_id")
        batch.drop_constraint(
            "fk_usage_events_child_run_id_execution_child_runs", type_="foreignkey"
        )
        batch.drop_column("child_run_id")
        batch.drop_column("source_key")
        batch.drop_column("source")
    op.drop_index("ix_execution_child_runs_role", table_name="execution_child_runs")
    op.drop_index("ix_execution_child_runs_timeline", table_name="execution_child_runs")
    op.drop_index("ux_execution_child_runs_source", table_name="execution_child_runs")
    op.drop_table("execution_child_runs")
