"""Add durable disposable-runner job state.

Revision ID: 20260916_0008
Revises: 20260909_0007
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0008"
down_revision: Union[str, Sequence[str], None] = "20260909_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runner_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("argv", sa.JSON(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("base_commit", sa.String(length=64), nullable=False),
        sa.Column("preflight_digest", sa.String(length=64), nullable=False),
        sa.Column("runner_image_id", sa.String(length=71), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=False),
        sa.Column("stderr", sa.Text(), nullable=False),
        sa.Column("output_truncated", sa.Boolean(), nullable=False),
        sa.Column("cleanup_confirmed", sa.Boolean(), nullable=True),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cleanup_uncertain', 'rejected')",
            name="ck_runner_jobs_status",
        ),
        sa.CheckConstraint("timeout_seconds BETWEEN 1 AND 3600", name="ck_runner_jobs_timeout"),
        sa.CheckConstraint("length(base_commit) IN (40, 64)", name="ck_runner_jobs_base_commit"),
        sa.CheckConstraint("length(preflight_digest) = 64", name="ck_runner_jobs_preflight_digest"),
        sa.CheckConstraint(
            "runner_image_id IS NULL OR length(runner_image_id) = 71",
            name="ck_runner_jobs_image_id",
        ),
        sa.CheckConstraint("length(idempotency_key) = 36", name="ck_runner_jobs_idempotency_key"),
        sa.CheckConstraint("lease_generation >= 0", name="ck_runner_jobs_lease_generation"),
        sa.CheckConstraint("failure_count >= 0", name="ck_runner_jobs_failure_count"),
        sa.CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_runner_jobs_lease_pair",
        ),
        sa.CheckConstraint(
            "((status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL))",
            name="ck_runner_jobs_active_lease",
        ),
        sa.CheckConstraint(
            "((status = 'queued' AND next_attempt_at IS NOT NULL) OR "
            "(status <> 'queued' AND next_attempt_at IS NULL))",
            name="ck_runner_jobs_queue_state",
        ),
        sa.CheckConstraint(
            "((status IN ('queued', 'running') AND finished_at IS NULL) OR "
            "(status NOT IN ('queued', 'running') AND finished_at IS NOT NULL))",
            name="ck_runner_jobs_terminal_time",
        ),
        sa.CheckConstraint(
            "((status IN ('completed', 'failed', 'timed_out') "
            "AND cleanup_confirmed = TRUE AND runner_image_id IS NOT NULL) OR "
            "(status = 'cleanup_uncertain' AND cleanup_confirmed = FALSE) OR "
            "(status = 'rejected' AND cleanup_confirmed = TRUE) OR "
            "(status IN ('queued', 'running') AND cleanup_confirmed IS NULL))",
            name="ck_runner_jobs_cleanup_state",
        ),
        sa.CheckConstraint(
            "((status IN ('completed', 'failed') AND exit_code IS NOT NULL) OR "
            "(status IN ('queued', 'running', 'timed_out', 'rejected') "
            "AND exit_code IS NULL) OR status = 'cleanup_uncertain')",
            name="ck_runner_jobs_exit_code",
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["execution_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["task_workspaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ux_runner_jobs_execution_idempotency", "runner_jobs", ["execution_id", "idempotency_key"], unique=True)
    op.create_index("ix_runner_jobs_queue", "runner_jobs", ["status", "next_attempt_at"], unique=False)
    op.create_index("ix_runner_jobs_lease", "runner_jobs", ["lease_expires_at"], unique=False)
    op.create_index("ix_runner_jobs_execution", "runner_jobs", ["execution_id", "status"], unique=False)
    op.create_index("ix_runner_jobs_repository", "runner_jobs", ["repository_id", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runner_jobs_repository", table_name="runner_jobs")
    op.drop_index("ix_runner_jobs_execution", table_name="runner_jobs")
    op.drop_index("ix_runner_jobs_lease", table_name="runner_jobs")
    op.drop_index("ix_runner_jobs_queue", table_name="runner_jobs")
    op.drop_index("ux_runner_jobs_execution_idempotency", table_name="runner_jobs")
    op.drop_table("runner_jobs")
