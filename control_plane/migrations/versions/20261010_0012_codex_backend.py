"""Persist the execution backend; existing runs retain their OpenCode owner.

Revision ID: 20261010_0012
Revises: 20260917_0011
"""
from alembic import op
import sqlalchemy as sa

revision = "20261010_0012"
down_revision = "20260917_0011"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("execution_runs") as batch:
        batch.add_column(sa.Column("backend", sa.String(length=32), nullable=False,
                                   server_default="opencode"))
        batch.create_check_constraint("ck_execution_runs_backend",
                                      "backend IN ('opencode', 'codex-subscription')")


def downgrade():
    connection = op.get_bind()
    count = connection.execute(sa.text(
        "SELECT count(*) FROM execution_runs WHERE backend = 'codex-subscription'"
    )).scalar_one()
    if count:
        raise RuntimeError("Codex execution history must be retained; downgrade refused")
    with op.batch_alter_table("execution_runs") as batch:
        batch.drop_constraint("ck_execution_runs_backend", type_="check")
        batch.drop_column("backend")
