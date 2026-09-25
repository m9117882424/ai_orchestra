"""Add G5.1 content-addressed controlled action authorization.

Revision ID: 20260925_0012
Revises: 20260917_0011
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260925_0012"
down_revision: Union[str, Sequence[str], None] = "20260917_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "controlled_actions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("source_sha", sa.String(length=64), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("destination", sa.String(length=512), nullable=False),
        sa.Column("result_package_digest", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("action_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action_type IN ('git_push', 'pull_request', 'merge', 'deploy', 'external_write')",
            name="ck_controlled_actions_type",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'pending_approval', 'approved', 'approval_rejected', "
            "'claimed', 'succeeded', 'failed', 'uncertain', 'reconciled')",
            name="ck_controlled_actions_status",
        ),
        sa.CheckConstraint(
            "length(source_sha) IN (40, 64) AND length(head_sha) IN (40, 64)",
            name="ck_controlled_actions_sha_length",
        ),
        sa.CheckConstraint(
            "result_package_digest IS NULL OR length(result_package_digest) = 64",
            name="ck_controlled_actions_result_package_digest",
        ),
        sa.CheckConstraint("length(action_digest) = 64", name="ck_controlled_actions_digest"),
        sa.CheckConstraint("version >= 1", name="ck_controlled_actions_version"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_controlled_actions_task_id"), "controlled_actions", ["task_id"], unique=False)
    op.create_index(
        "ix_controlled_actions_repository_status",
        "controlled_actions",
        ["repository_id", "status"],
        unique=False,
    )
    op.create_index(
        "ux_controlled_actions_digest",
        "controlled_actions",
        ["action_digest"],
        unique=True,
    )

    op.create_table(
        "controlled_action_authorizations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("action_id", sa.String(length=36), nullable=False),
        sa.Column("action_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.String(length=100), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'consumed')",
            name="ck_controlled_action_authorizations_status",
        ),
        sa.CheckConstraint(
            "length(action_digest) = 64",
            name="ck_controlled_action_authorizations_digest",
        ),
        sa.CheckConstraint(
            "((status IN ('approved', 'rejected', 'consumed') AND decided_at IS NOT NULL "
            "AND decided_by IS NOT NULL) OR status IN ('pending', 'expired'))",
            name="ck_controlled_action_authorizations_decision",
        ),
        sa.CheckConstraint(
            "((status = 'consumed' AND consumed_at IS NOT NULL AND consumed_by IS NOT NULL "
            "AND operation_key IS NOT NULL) OR (status <> 'consumed' AND consumed_at IS NULL "
            "AND consumed_by IS NULL AND operation_key IS NULL))",
            name="ck_controlled_action_authorizations_consumption",
        ),
        sa.ForeignKeyConstraint(["action_id"], ["controlled_actions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_controlled_action_authorizations_action_status",
        "controlled_action_authorizations",
        ["action_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ux_controlled_action_authorizations_operation_key",
        "controlled_action_authorizations",
        ["operation_key"],
        unique=True,
    )

    op.create_table(
        "controlled_action_effects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("action_id", sa.String(length=36), nullable=False),
        sa.Column("authorization_id", sa.String(length=36), nullable=False),
        sa.Column("action_digest", sa.String(length=64), nullable=False),
        sa.Column("operation_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_by", sa.String(length=100), nullable=False),
        sa.Column("external_ref", sa.String(length=512), nullable=True),
        sa.Column("observed_before_digest", sa.String(length=64), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preflight_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved', 'succeeded', 'failed', 'uncertain', 'reconciled')",
            name="ck_controlled_action_effects_status",
        ),
        sa.CheckConstraint("length(action_digest) = 64", name="ck_controlled_action_effects_digest"),
        sa.CheckConstraint(
            "observed_before_digest IS NULL OR length(observed_before_digest) IN (40, 64)",
            name="ck_controlled_action_effects_before_digest",
        ),
        sa.CheckConstraint(
            "result_digest IS NULL OR length(result_digest) IN (40, 64)",
            name="ck_controlled_action_effects_result_digest",
        ),
        sa.ForeignKeyConstraint(["action_id"], ["controlled_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["controlled_action_authorizations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_controlled_action_effects_action",
        "controlled_action_effects",
        ["action_id"],
        unique=True,
    )
    op.create_index(
        "ux_controlled_action_effects_operation_key",
        "controlled_action_effects",
        ["operation_key"],
        unique=True,
    )
    op.create_index(
        "ix_controlled_action_effects_status",
        "controlled_action_effects",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_controlled_action_effects_status", table_name="controlled_action_effects")
    op.drop_index("ux_controlled_action_effects_operation_key", table_name="controlled_action_effects")
    op.drop_index("ux_controlled_action_effects_action", table_name="controlled_action_effects")
    op.drop_table("controlled_action_effects")

    op.drop_index(
        "ux_controlled_action_authorizations_operation_key",
        table_name="controlled_action_authorizations",
    )
    op.drop_index(
        "ix_controlled_action_authorizations_action_status",
        table_name="controlled_action_authorizations",
    )
    op.drop_table("controlled_action_authorizations")

    op.drop_index("ux_controlled_actions_digest", table_name="controlled_actions")
    op.drop_index("ix_controlled_actions_repository_status", table_name="controlled_actions")
    op.drop_index(op.f("ix_controlled_actions_task_id"), table_name="controlled_actions")
    op.drop_table("controlled_actions")
