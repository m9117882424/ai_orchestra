from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text

from control_plane.app.database_base import Base
import control_plane.app.models  # noqa: F401
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


def _run_alembic_upgrade(database_url: str, revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", revision],
        cwd=CONTROL_PLANE_ROOT,
        env=_schema_env(database_url),
        text=True,
        capture_output=True,
        check=False,
    )


def _run_production_startup(database_url: str) -> subprocess.CompletedProcess[str]:
    code = """
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as client:
    response = client.get('/health')
    assert response.status_code == 200
print('STARTED')
"""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=CONTROL_PLANE_ROOT,
        env=_production_env(database_url),
        text=True,
        capture_output=True,
        check=False,
    )


def test_declared_schema_head_is_stable():
    assert head_revision() == "20260905_0003"


def test_fresh_database_is_created_by_alembic(tmp_path):
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite+pysqlite:///{database_path}"

    result = _run_schema_cli(database_url, "migrate")
    assert result.returncode == 0, result.stderr

    engine = create_engine(database_url)
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        session_column = next(
            column
            for column in inspect(connection).get_columns("execution_runs")
            if column["name"] == "opencode_session_id"
        )

    assert set(Base.metadata.tables).issubset(tables)
    assert revision == "20260905_0003"
    assert session_column["nullable"] is True


def test_matching_current_unversioned_database_is_verified_then_stamped(tmp_path):
    database_path = tmp_path / "legacy-current.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    assert legacy_schema_diff(engine) == []

    result = _run_schema_cli(database_url, "migrate")
    assert result.returncode == 0, result.stderr
    assert "data unchanged" in result.stdout

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == "20260905_0003"


def test_unversioned_historical_baseline_is_verified_then_migrated(tmp_path):
    database_path = tmp_path / "legacy-baseline.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    created = _run_alembic_upgrade(database_url, "20260904_0001")
    assert created.returncode == 0, created.stderr

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, title, description, project, domain, priority, status,
                    risk_level, owner_role, created_at, updated_at
                ) VALUES (
                    'legacy-task', 'Legacy marker', '', 'general', 'development',
                    'normal', 'backlog', 'low', NULL,
                    '2026-09-05 00:00:00', '2026-09-05 00:00:00'
                )
                """
            )
        )
        connection.exec_driver_sql("DROP TABLE alembic_version")

    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr
    assert "Historical baseline 20260904_0001 verified" in migrated.stdout

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        marker = connection.execute(
            text("SELECT title FROM tasks WHERE id = 'legacy-task'")
        ).scalar_one()
        execution_columns = {
            column["name"] for column in inspect(connection).get_columns("execution_runs")
        }
        session_column = next(
            column
            for column in inspect(connection).get_columns("execution_runs")
            if column["name"] == "opencode_session_id"
        )

    assert revision == "20260905_0003"
    assert marker == "Legacy marker"
    assert {
        "lease_owner",
        "lease_generation",
        "heartbeat_at",
        "lease_expires_at",
    }.issubset(execution_columns)
    assert session_column["nullable"] is True


def test_versioned_0002_database_upgrades_to_queued_dispatch_schema(tmp_path):
    database_path = tmp_path / "revision-0002.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    created = _run_alembic_upgrade(database_url, "20260905_0002")
    assert created.returncode == 0, created.stderr

    engine = create_engine(database_url)
    with engine.connect() as connection:
        before = next(
            column
            for column in inspect(connection).get_columns("execution_runs")
            if column["name"] == "opencode_session_id"
        )
    assert before["nullable"] is False

    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        after = next(
            column
            for column in inspect(connection).get_columns("execution_runs")
            if column["name"] == "opencode_session_id"
        )
    assert revision == "20260905_0003"
    assert after["nullable"] is True


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


def test_production_application_starts_on_migrated_schema(tmp_path):
    database_path = tmp_path / "application.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    started = _run_production_startup(database_url)
    assert started.returncode == 0, started.stderr
    assert "STARTED" in started.stdout


def test_production_application_never_repairs_schema_drift(tmp_path):
    database_path = tmp_path / "drift-after-version.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE audit_events")

    started = _run_production_startup(database_url)
    assert started.returncode != 0
    assert "schema drift detected" in started.stderr
    assert "missing table: audit_events" in started.stderr


def test_production_application_detects_index_drift_with_valid_revision(tmp_path):
    database_path = tmp_path / "index-drift.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_execution_runs_task_id")

    started = _run_production_startup(database_url)
    assert started.returncode != 0
    assert "schema drift detected" in started.stderr
    assert "indexes" in started.stderr
