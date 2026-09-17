"""Add G4 durable execution evidence and result packages.

Revision ID: 20260917_0010
Revises: 20260916_0009
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260917_0010"
down_revision: Union[str, Sequence[str], None] = "20260916_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("usage_events") as batch:
        batch.add_column(sa.Column("execution_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_usage_events_execution_id_execution_runs",
            "execution_runs",
            ["execution_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_usage_events_execution_id", ["execution_id"], unique=False)

    op.create_table(
        "execution_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("tool_name", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("retry_of_id", sa.String(length=36), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('stage', 'message', 'tool', 'runner', 'usage', 'error', 'retry', 'review', 'artifact')",
            name="ck_execution_evidence_kind",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_execution_evidence_attempt"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["execution_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_id"], ["execution_evidence.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_execution_evidence_source_key",
        "execution_evidence",
        ["execution_id", "source", "source_key"],
        unique=True,
    )
    op.create_index(
        "ix_execution_evidence_timeline",
        "execution_evidence",
        ["execution_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_evidence_kind",
        "execution_evidence",
        ["execution_id", "kind", "status"],
        unique=False,
    )

    op.create_table(
        "execution_result_packages",
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("package_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("package_digest", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "package_version = 1", name="ck_execution_result_packages_version"
        ),
        sa.CheckConstraint(
            "state IN ('provisional', 'final')",
            name="ck_execution_result_packages_state",
        ),
        sa.CheckConstraint(
            "length(package_digest) = 64",
            name="ck_execution_result_packages_digest",
        ),
        sa.CheckConstraint(
            "((state = 'final' AND finalized_at IS NOT NULL) OR "
            "(state = 'provisional' AND finalized_at IS NULL))",
            name="ck_execution_result_packages_final_state",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["execution_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("execution_id"),
    )


def downgrade() -> None:
    op.drop_table("execution_result_packages")
    op.drop_index("ix_execution_evidence_kind", table_name="execution_evidence")
    op.drop_index("ix_execution_evidence_timeline", table_name="execution_evidence")
    op.drop_index("ux_execution_evidence_source_key", table_name="execution_evidence")
    op.drop_table("execution_evidence")
    with op.batch_alter_table("usage_events") as batch:
        batch.drop_index("ix_usage_events_execution_id")
        batch.drop_constraint(
            "fk_usage_events_execution_id_execution_runs", type_="foreignkey"
        )
        batch.drop_column("execution_id")
