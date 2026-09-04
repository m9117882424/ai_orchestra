from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text

from control_plane.app.db import Base
from control_plane.app.schema import head_revision, legacy_schema_diff


CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]


def _schema_env(database_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "CONTROL_PLANE_ENVIRONMENT": "test",
            "CONTROL_PLANE_DATABASE_URL": database_url,
            "PYTHONPATH": str(CONTROL_PLANE_ROOT),
        }
    )
    return env


def _production_env(database_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "CONTROL_PLANE_ENVIRONMENT": "production",
            "CONTROL_PLANE_DATABASE_URL": database_url,
            "CONTROL_PLANE_SERVER_PASSWORD": "manager-password-for-schema-tests",
            "CONTROL_PLANE_OPENCODE_PASSWORD": "opencode-password-for-schema-tests",
            "PYTHONPATH": str(CONTROL_PLANE_ROOT),
        }
    )
    env.pop("CONTROL_PLANE_SCHEMA_MODE", None)
    return env


def _run_schema_cli(database_url: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.schema_cli", command],
        cwd=CONTROL_PLANE_ROOT,
        env=_schema_env(database_url),
        text=True,
        capture_output=True,
        check=False,
    )


def test_declared_schema_head_is_stable():
    assert head_revision() == "20260904_0001"


def test_fresh_database_is_created_by_alembic(tmp_path):
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite+pysqlite:///{database_path}"

    result = _run_schema_cli(database_url, "migrate")
    assert result.returncode == 0, result.stderr

    engine = create_engine(database_url)
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert set(Base.metadata.tables).issubset(tables)
    assert revision == "20260904_0001"


def test_matching_legacy_database_is_verified_then_stamped(tmp_path):
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    assert legacy_schema_diff(engine) == []

    result = _run_schema_cli(database_url, "migrate")
    assert result.returncode == 0, result.stderr
    assert "data unchanged" in result.stdout

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == "20260904_0001"


def test_drifted_legacy_database_is_never_stamped(tmp_path):
    database_path = tmp_path / "drift.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE audit_events")

    result = _run_schema_cli(database_url, "migrate")
    assert result.returncode != 0
    assert "refusing stamp" in result.stderr

    with engine.connect() as connection:
        assert "alembic_version" not in inspect(connection).get_table_names()


def test_production_runtime_refuses_unversioned_database(tmp_path):
    database_path = tmp_path / "unversioned.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)

    result = subprocess.run(
        [sys.executable, "-c", "import app.db"],
        cwd=CONTROL_PLANE_ROOT,
        env=_production_env(database_url),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unversioned" in result.stderr


def test_production_runtime_blocks_ddl_after_valid_migration(tmp_path):
    database_path = tmp_path / "runtime.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    code = """
from app.db import engine
with engine.begin() as connection:
    connection.exec_driver_sql('CREATE TABLE forbidden_runtime_ddl (id INTEGER)')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=CONTROL_PLANE_ROOT,
        env=_production_env(database_url),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Runtime DDL" in result.stderr
