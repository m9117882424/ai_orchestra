"""Add the fail-closed repository registry.

Revision ID: 20260908_0005
Revises: 20260907_0004
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260908_0005"
down_revision: Union[str, Sequence[str], None] = "20260907_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("remote_url", sa.String(length=1024), nullable=False),
        sa.Column("remote_identity", sa.String(length=1024), nullable=False),
        sa.Column("remote_host", sa.String(length=253), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("auth_profile_ref", sa.String(length=80), nullable=True),
        sa.Column("default_branch", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending_validation",
            nullable=False,
        ),
        sa.Column("last_known_commit", sa.String(length=64), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "execution_profile",
            sa.String(length=40),
            server_default="development",
            nullable=False,
        ),
        sa.Column(
            "assurance_tier",
            sa.String(length=40),
            server_default="general-standard",
            nullable=False,
        ),
        sa.Column("assurance_profile", sa.String(length=80), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'bitbucket', 'generic')",
            name="ck_repositories_provider",
        ),
        sa.CheckConstraint(
            "status IN ('pending_validation', 'ready', 'unavailable', 'invalid')",
            name="ck_repositories_status",
        ),
        sa.CheckConstraint(
            "execution_profile = 'development'",
            name="ck_repositories_execution_profile",
        ),
        sa.CheckConstraint(
            "assurance_tier IN "
            "('general-standard', 'general-high-assurance', 'regulated-critical')",
            name="ck_repositories_assurance_tier",
        ),
        sa.CheckConstraint(
            "((assurance_tier = 'regulated-critical' AND assurance_profile IS NOT NULL "
            "AND length(assurance_profile) BETWEEN 1 AND 80) "
            "OR (assurance_tier <> 'regulated-critical' AND assurance_profile IS NULL))",
            name="ck_repositories_assurance_profile",
        ),
        sa.CheckConstraint("version >= 1", name="ck_repositories_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ux_repositories_name", "repositories", ["name"], unique=True)
    op.create_index(
        "ux_repositories_remote_identity",
        "repositories",
        ["remote_identity"],
        unique=True,
    )
    op.create_index(
        "ix_repositories_enabled_status",
        "repositories",
        ["enabled", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_repositories_enabled_status", table_name="repositories")
    op.drop_index("ux_repositories_remote_identity", table_name="repositories")
    op.drop_index("ux_repositories_name", table_name="repositories")
    op.drop_table("repositories")
