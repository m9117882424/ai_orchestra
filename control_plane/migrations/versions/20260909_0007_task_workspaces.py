"""Add durable task workspaces and immutable execution bindings.

Revision ID: 20260909_0007
Revises: 20260909_0006
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260909_0007"
down_revision: Union[str, Sequence[str], None] = "20260909_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column("repository_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_tasks_repository_id_repositories",
            "repositories",
            ["repository_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_tasks_repository_id", "tasks", ["repository_id"], unique=False
    )

    op.create_table(
        "task_workspaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("base_commit", sa.String(length=64), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("branch_name", sa.String(length=255), nullable=False),
        sa.Column("opencode_path", sa.String(length=512), nullable=False),
        sa.Column("initial_tree", sa.String(length=64), nullable=True),
        sa.Column("preflight_digest", sa.String(length=64), nullable=True),
        sa.Column("tracked_entries", sa.Integer(), nullable=True),
        sa.Column("current_head_commit", sa.String(length=64), nullable=True),
        sa.Column("current_tree", sa.String(length=64), nullable=True),
        sa.Column("change_digest", sa.String(length=64), nullable=True),
        sa.Column("has_changes", sa.Boolean(), nullable=True),
        sa.Column("changed_file_count", sa.Integer(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inspection_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'preparing', 'unavailable', 'ready', "
            "'inspection_pending', 'inspecting', 'retained', "
            "'cleanup_pending', 'cleaning', 'removed', 'invalid')",
            name="ck_task_workspaces_status",
        ),
        sa.CheckConstraint(
            "length(base_commit) IN (40, 64)",
            name="ck_task_workspaces_base_commit",
        ),
        sa.CheckConstraint(
            "length(base_branch) BETWEEN 1 AND 255",
            name="ck_task_workspaces_base_branch",
        ),
        sa.CheckConstraint(
            "length(branch_name) BETWEEN 1 AND 255",
            name="ck_task_workspaces_branch_name",
        ),
        sa.CheckConstraint(
            "length(opencode_path) BETWEEN 1 AND 512",
            name="ck_task_workspaces_opencode_path",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="ck_task_workspaces_generation"
        ),
        sa.CheckConstraint(
            "failure_count >= 0", name="ck_task_workspaces_failure_count"
        ),
        sa.CheckConstraint("version >= 1", name="ck_task_workspaces_version"),
        sa.CheckConstraint(
            "changed_file_count IS NULL OR changed_file_count >= 0",
            name="ck_task_workspaces_changed_file_count",
        ),
        sa.CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_task_workspaces_lease_pair",
        ),
        sa.CheckConstraint(
            "((status IN ('preparing', 'inspecting', 'cleaning') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status NOT IN ('preparing', 'inspecting', 'cleaning') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL))",
            name="ck_task_workspaces_active_lease",
        ),
        sa.CheckConstraint(
            "((status IN ('pending', 'unavailable', 'inspection_pending', "
            "'cleanup_pending') AND next_attempt_at IS NOT NULL) "
            "OR (status NOT IN ('pending', 'unavailable', 'inspection_pending', "
            "'cleanup_pending') AND next_attempt_at IS NULL))",
            name="ck_task_workspaces_queue_state",
        ),
        sa.CheckConstraint(
            "(status <> 'ready' OR (initial_tree IS NOT NULL "
            "AND length(initial_tree) IN (40, 64) "
            "AND preflight_digest IS NOT NULL "
            "AND length(preflight_digest) = 64 AND tracked_entries IS NOT NULL "
            "AND tracked_entries >= 0 "
            "AND prepared_at IS NOT NULL AND last_error_code IS NULL))",
            name="ck_task_workspaces_ready_state",
        ),
        sa.CheckConstraint(
            "(status <> 'retained' OR (inspected_at IS NOT NULL "
            "AND has_changes IS NOT NULL AND changed_file_count IS NOT NULL))",
            name="ck_task_workspaces_retained_state",
        ),
        sa.CheckConstraint(
            "(status <> 'removed' OR cleaned_at IS NOT NULL)",
            name="ck_task_workspaces_removed_state",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["repositories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_task_workspaces_opencode_path",
        "task_workspaces",
        ["opencode_path"],
        unique=True,
    )
    op.create_index(
        "ix_task_workspaces_task_id", "task_workspaces", ["task_id"], unique=False
    )
    op.create_index(
        "ix_task_workspaces_repository_id",
        "task_workspaces",
        ["repository_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_workspaces_queue",
        "task_workspaces",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_workspaces_lease",
        "task_workspaces",
        ["lease_expires_at"],
        unique=False,
    )

    with op.batch_alter_table("execution_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "contract_version",
                sa.Integer(),
                server_default="1",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("repository_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("workspace_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("base_commit", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("workspace_path", sa.String(length=512), nullable=True)
        )
        batch_op.add_column(
            sa.Column("workspace_tree", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "workspace_preflight_digest", sa.String(length=64), nullable=True
            )
        )
        batch_op.add_column(
            sa.Column(
                "workspace_preflight_completed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "workspace_runtime_preflight_digest",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "workspace_runtime_verified_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_execution_runs_repository_id_repositories",
            "repositories",
            ["repository_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_execution_runs_workspace_id_task_workspaces",
            "task_workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_execution_runs_contract_version",
            "contract_version IN (1, 2)",
        )
        batch_op.create_check_constraint(
            "ck_execution_runs_workspace_binding",
            "(contract_version = 1 OR (repository_id IS NOT NULL "
            "AND workspace_id IS NOT NULL AND base_commit IS NOT NULL "
            "AND length(base_commit) IN (40, 64) "
            "AND workspace_path IS NOT NULL "
            "AND length(workspace_path) BETWEEN 1 AND 512))",
        )
        batch_op.create_check_constraint(
            "ck_execution_runs_workspace_preflight",
            "(contract_version = 1 OR status NOT IN ('queued', 'running', 'completed') "
            "OR (workspace_tree IS NOT NULL "
            "AND length(workspace_tree) IN (40, 64) "
            "AND workspace_preflight_digest IS NOT NULL "
            "AND length(workspace_preflight_digest) = 64 "
            "AND workspace_preflight_completed_at IS NOT NULL))",
        )
        batch_op.create_check_constraint(
            "ck_execution_runs_workspace_runtime_preflight",
            "(contract_version = 1 OR status NOT IN ('running', 'completed') "
            "OR (workspace_runtime_preflight_digest IS NOT NULL "
            "AND length(workspace_runtime_preflight_digest) = 64 "
            "AND workspace_runtime_verified_at IS NOT NULL))",
        )
    op.create_index(
        "ix_execution_runs_repository_id",
        "execution_runs",
        ["repository_id"],
        unique=False,
    )
    op.create_index(
        "ux_execution_runs_workspace_id",
        "execution_runs",
        ["workspace_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_execution_runs_workspace_id", table_name="execution_runs")
    op.drop_index("ix_execution_runs_repository_id", table_name="execution_runs")
    with op.batch_alter_table("execution_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_execution_runs_workspace_runtime_preflight", type_="check"
        )
        batch_op.drop_constraint(
            "ck_execution_runs_workspace_preflight", type_="check"
        )
        batch_op.drop_constraint(
            "ck_execution_runs_workspace_binding", type_="check"
        )
        batch_op.drop_constraint(
            "ck_execution_runs_contract_version", type_="check"
        )
        batch_op.drop_constraint(
            "fk_execution_runs_workspace_id_task_workspaces", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_execution_runs_repository_id_repositories", type_="foreignkey"
        )
        batch_op.drop_column("workspace_preflight_completed_at")
        batch_op.drop_column("workspace_preflight_digest")
        batch_op.drop_column("workspace_runtime_verified_at")
        batch_op.drop_column("workspace_runtime_preflight_digest")
        batch_op.drop_column("workspace_tree")
        batch_op.drop_column("workspace_path")
        batch_op.drop_column("base_commit")
        batch_op.drop_column("workspace_id")
        batch_op.drop_column("repository_id")
        batch_op.drop_column("contract_version")

    op.drop_index("ix_task_workspaces_lease", table_name="task_workspaces")
    op.drop_index("ix_task_workspaces_queue", table_name="task_workspaces")
    op.drop_index("ix_task_workspaces_repository_id", table_name="task_workspaces")
    op.drop_index("ix_task_workspaces_task_id", table_name="task_workspaces")
    op.drop_index("ux_task_workspaces_opencode_path", table_name="task_workspaces")
    op.drop_table("task_workspaces")

    op.drop_index("ix_tasks_repository_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(
            "fk_tasks_repository_id_repositories", type_="foreignkey"
        )
        batch_op.drop_column("repository_id")
