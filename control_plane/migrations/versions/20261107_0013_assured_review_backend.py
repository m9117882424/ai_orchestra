"""Add an explicitly isolated high-assurance OpenCode review backend.

Revision ID: 20261107_0013
Revises: 20261010_0012
"""
from alembic import op
import sqlalchemy as sa

revision = "20261107_0013"
down_revision = "20261010_0012"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("execution_runs") as batch:
        batch.drop_constraint("ck_execution_runs_backend", type_="check")
        batch.create_check_constraint(
            "ck_execution_runs_backend",
            "backend IN ('opencode', 'codex-subscription', 'opencode-review')",
        )


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM execution_runs WHERE backend = 'opencode-review'"
    )).scalar_one()
    if count:
        raise RuntimeError("Assured review history must be retained; downgrade refused")
    with op.batch_alter_table("execution_runs") as batch:
        batch.drop_constraint("ck_execution_runs_backend", type_="check")
        batch.create_check_constraint(
            "ck_execution_runs_backend", "backend IN ('opencode', 'codex-subscription')"
        )
