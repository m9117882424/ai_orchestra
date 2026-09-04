"""Initial Control Plane schema baseline.

Revision ID: 20260904_0001
Revises: None
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("project", sa.String(length=120), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("owner_role", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "budgets",
        sa.Column("scope", sa.String(length=80), nullable=False),
        sa.Column("monthly_limit", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("warning_pct", sa.Integer(), nullable=False),
        sa.Column("hard_stop", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=80), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "capability_guard",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("production_deploy_allowed", sa.Boolean(), nullable=False),
        sa.Column("external_write_allowed", sa.Boolean(), nullable=False),
        sa.Column("financial_execution_allowed", sa.Boolean(), nullable=False),
        sa.Column("secret_access_allowed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approvals_task_id"), "approvals", ["task_id"], unique=False)
    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_usage_events_task_id"), "usage_events", ["task_id"], unique=False)
    op.create_table(
        "execution_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("opencode_session_id", sa.String(length=120), nullable=False),
        sa.Column("lead_role", sa.String(length=80), nullable=False),
        sa.Column("assigned_roles", sa.JSON(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_execution_runs_task_id"), "execution_runs", ["task_id"], unique=False)
    op.create_index(
        op.f("ix_execution_runs_opencode_session_id"),
        "execution_runs",
        ["opencode_session_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_runs_opencode_session_id"), table_name="execution_runs")
    op.drop_index(op.f("ix_execution_runs_task_id"), table_name="execution_runs")
    op.drop_table("execution_runs")
    op.drop_index(op.f("ix_usage_events_task_id"), table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index(op.f("ix_approvals_task_id"), table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("capability_guard")
    op.drop_table("audit_events")
    op.drop_table("budgets")
    op.drop_table("tasks")
