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
    assert head_revision() == "20260916_0008"


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
        execution_columns = {
            column["name"] for column in inspect(connection).get_columns("execution_runs")
        }
        execution_indexes = {
            tuple(index["column_names"])
            for index in inspect(connection).get_indexes("execution_runs")
        }
        repository_columns = {
            column["name"] for column in inspect(connection).get_columns("repositories")
        }
        repository_indexes = {
            (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspect(connection).get_indexes("repositories")
        }
        repository_checks = {
            constraint["name"]
            for constraint in inspect(connection).get_check_constraints("repositories")
        }
        workspace_columns = {
            column["name"] for column in inspect(connection).get_columns("task_workspaces")
        }
        workspace_indexes = {
            (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspect(connection).get_indexes("task_workspaces")
        }
        workspace_checks = {
            constraint["name"]
            for constraint in inspect(connection).get_check_constraints("task_workspaces")
        }
        execution_checks = {
            constraint["name"]
            for constraint in inspect(connection).get_check_constraints("execution_runs")
        }
        runner_columns = {
            column["name"] for column in inspect(connection).get_columns("runner_jobs")
        }
        runner_indexes = {
            (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspect(connection).get_indexes("runner_jobs")
        }
        runner_checks = {
            constraint["name"]
            for constraint in inspect(connection).get_check_constraints("runner_jobs")
        }

    assert set(Base.metadata.tables).issubset(tables)
    assert revision == "20260916_0008"
    assert session_column["nullable"] is True
    assert "deadline_at" in execution_columns
    assert "cancel_requested_at" in execution_columns
    assert {
        "contract_version",
        "repository_id",
        "workspace_id",
        "base_commit",
        "workspace_path",
        "workspace_tree",
        "workspace_preflight_digest",
        "workspace_preflight_completed_at",
        "workspace_runtime_preflight_digest",
        "workspace_runtime_verified_at",
    }.issubset(execution_columns)
    assert ("status", "deadline_at") in execution_indexes
    assert {
        "ck_execution_runs_contract_version",
        "ck_execution_runs_workspace_binding",
        "ck_execution_runs_workspace_preflight",
        "ck_execution_runs_workspace_runtime_preflight",
    }.issubset(execution_checks)
    assert {
        "remote_identity",
        "remote_host",
        "auth_profile_ref",
        "last_known_commit",
        "last_fetched_at",
        "sync_generation",
        "sync_failure_count",
        "sync_requested_at",
        "sync_started_at",
        "sync_finished_at",
        "sync_next_at",
        "sync_lease_owner",
        "sync_lease_expires_at",
        "last_sync_error_code",
        "assurance_tier",
        "assurance_profile",
        "version",
    }.issubset(repository_columns)
    assert (("name",), True) in repository_indexes
    assert (("remote_identity",), True) in repository_indexes
    assert (("enabled", "status"), False) in repository_indexes
    assert (("enabled", "sync_next_at"), False) in repository_indexes
    assert (("sync_lease_expires_at",), False) in repository_indexes
    assert repository_checks == {
        "ck_repositories_assurance_profile",
        "ck_repositories_assurance_tier",
        "ck_repositories_execution_profile",
        "ck_repositories_provider",
        "ck_repositories_ready_state",
        "ck_repositories_status",
        "ck_repositories_sync_failure_count",
        "ck_repositories_sync_generation",
        "ck_repositories_sync_lease_pair",
        "ck_repositories_validating_state",
        "ck_repositories_version",
    }
    assert {
        "task_id",
        "repository_id",
        "status",
        "base_commit",
        "branch_name",
        "opencode_path",
        "initial_tree",
        "preflight_digest",
        "tracked_entries",
        "generation",
        "lease_owner",
        "lease_expires_at",
    }.issubset(workspace_columns)
    assert (("opencode_path",), True) in workspace_indexes
    assert (("status", "next_attempt_at"), False) in workspace_indexes
    assert {
        "ck_task_workspaces_status",
        "ck_task_workspaces_active_lease",
        "ck_task_workspaces_queue_state",
        "ck_task_workspaces_ready_state",
        "ck_task_workspaces_retained_state",
        "ck_task_workspaces_removed_state",
    }.issubset(workspace_checks)
    assert {
        "execution_id",
        "repository_id",
        "workspace_id",
        "idempotency_key",
        "status",
        "argv",
        "timeout_seconds",
        "base_commit",
        "preflight_digest",
        "runner_image_id",
        "cleanup_confirmed",
        "lease_owner",
        "lease_generation",
        "lease_expires_at",
        "next_attempt_at",
    }.issubset(runner_columns)
    assert (("execution_id", "idempotency_key"), True) in runner_indexes
    assert (("status", "next_attempt_at"), False) in runner_indexes
    assert (("lease_expires_at",), False) in runner_indexes
    assert {
        "ck_runner_jobs_status",
        "ck_runner_jobs_timeout",
        "ck_runner_jobs_base_commit",
        "ck_runner_jobs_preflight_digest",
        "ck_runner_jobs_image_id",
        "ck_runner_jobs_idempotency_key",
        "ck_runner_jobs_lease_generation",
        "ck_runner_jobs_failure_count",
        "ck_runner_jobs_lease_pair",
        "ck_runner_jobs_active_lease",
        "ck_runner_jobs_queue_state",
        "ck_runner_jobs_terminal_time",
        "ck_runner_jobs_cleanup_state",
        "ck_runner_jobs_exit_code",
    }.issubset(runner_checks)


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
    assert revision == "20260916_0008"


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

    assert revision == "20260916_0008"
    assert marker == "Legacy marker"
    assert {
        "lease_owner",
        "lease_generation",
        "heartbeat_at",
        "lease_expires_at",
        "deadline_at",
        "cancel_requested_at",
    }.issubset(execution_columns)
    assert session_column["nullable"] is True
    assert "repositories" in inspect(engine).get_table_names()


def test_versioned_0002_database_upgrades_to_current_execution_schema(tmp_path):
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
        execution_columns = {
            column["name"] for column in inspect(connection).get_columns("execution_runs")
        }
    assert revision == "20260916_0008"
    assert after["nullable"] is True
    assert "deadline_at" in execution_columns
    assert "cancel_requested_at" in execution_columns


def test_versioned_0003_backfills_only_active_execution_deadlines(tmp_path):
    database_path = tmp_path / "revision-0003.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    created = _run_alembic_upgrade(database_url, "20260905_0003")
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
                    'deadline-task', 'Deadline migration', '', 'general',
                    'development', 'normal', 'in_progress', 'low', NULL,
                    '2026-09-07 00:00:00', '2026-09-07 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_runs (
                    id, task_id, status, stage, opencode_session_id, lead_role,
                    assigned_roles, result, error, lease_generation,
                    created_at, updated_at, finished_at
                ) VALUES
                (
                    'queued-run', 'deadline-task', 'queued', 'dispatch_pending',
                    NULL, 'department-lead', '[]', '', '', 0,
                    '2026-09-07 00:00:00', '2026-09-07 00:00:00', NULL
                ),
                (
                    'completed-run', 'deadline-task', 'completed', 'manager_review',
                    'completed-session', 'department-lead', '[]', 'done', '', 0,
                    '2026-09-07 00:00:00', '2026-09-07 00:05:00',
                    '2026-09-07 00:05:00'
                )
                """
            )
        )

    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        deadlines = dict(
            connection.execute(
                text("SELECT id, deadline_at FROM execution_runs ORDER BY id")
            ).all()
        )

    assert revision == "20260916_0008"
    assert deadlines["queued-run"] is not None
    assert deadlines["completed-run"] is None


def test_versioned_0004_adds_registry_without_mutating_existing_data(tmp_path):
    database_path = tmp_path / "revision-0004.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    created = _run_alembic_upgrade(database_url, "20260907_0004")
    assert created.returncode == 0, created.stderr

    engine = create_engine(database_url)
    with engine.begin() as connection:
        assert "repositories" not in inspect(connection).get_table_names()
        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, actor, action, entity_type, entity_id, details, created_at
                ) VALUES (
                    'registry-migration-marker', 'test', 'marker', 'test',
                    'registry-migration-marker', '{}', '2026-09-08 00:00:00'
                )
                """
            )
        )

    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        marker = connection.execute(
            text("SELECT action FROM audit_events WHERE id = 'registry-migration-marker'")
        ).scalar_one()
        tables = set(inspect(connection).get_table_names())

    assert revision == "20260916_0008"
    assert marker == "marker"
    assert "repositories" in tables


def test_versioned_0005_registry_is_requeued_without_losing_policy(tmp_path):
    database_path = tmp_path / "revision-0005.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    created = _run_alembic_upgrade(database_url, "20260908_0005")
    assert created.returncode == 0, created.stderr

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO repositories (
                    id, name, remote_url, remote_identity, remote_host, provider,
                    auth_profile_ref, default_branch, enabled, status,
                    last_known_commit, last_fetched_at, execution_profile,
                    assurance_tier, assurance_profile, version, created_at, updated_at
                ) VALUES (
                    '00000000-0000-4000-8000-000000000005', 'existing-repository',
                    'https://github.com/example/existing.git',
                    'github.com/example/existing', 'github.com', 'github',
                    'git-readonly', 'main', 1, 'ready',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    '2026-09-09 00:00:00', 'development',
                    'general-high-assurance', NULL, 7,
                    '2026-09-08 00:00:00', '2026-09-08 00:00:00'
                )
                """
            )
        )

    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        row = connection.execute(
            text(
                """
                SELECT name, remote_identity, auth_profile_ref, assurance_tier,
                       version, status, sync_generation, sync_failure_count,
                       sync_requested_at, sync_next_at
                FROM repositories
                WHERE id = '00000000-0000-4000-8000-000000000005'
                """
            )
        ).one()

    assert revision == "20260916_0008"
    assert tuple(row[:5]) == (
        "existing-repository",
        "github.com/example/existing",
        "git-readonly",
        "general-high-assurance",
        7,
    )
    assert row.status == "pending_validation"
    assert row.sync_generation == 0
    assert row.sync_failure_count == 0
    assert row.sync_requested_at is not None
    assert row.sync_next_at is not None


def test_versioned_0006_preserves_legacy_execution_as_contract_v1(tmp_path):
    database_path = tmp_path / "revision-0006.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    created = _run_alembic_upgrade(database_url, "20260909_0006")
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
                    'legacy-contract-task', 'Legacy contract marker', '', 'general',
                    'development', 'normal', 'in_progress', 'low', NULL,
                    '2026-09-09 00:00:00', '2026-09-09 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_runs (
                    id, task_id, status, stage, opencode_session_id, lead_role,
                    assigned_roles, result, error, lease_generation, deadline_at,
                    created_at, updated_at, finished_at
                ) VALUES (
                    'legacy-contract-run', 'legacy-contract-task', 'queued',
                    'dispatch_pending', NULL, 'department-lead', '[]', '', '', 0,
                    '2026-09-09 02:00:00',
                    '2026-09-09 00:00:00', '2026-09-09 00:00:00', NULL
                )
                """
            )
        )

    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        task_repository = connection.execute(
            text("SELECT repository_id FROM tasks WHERE id = 'legacy-contract-task'")
        ).scalar_one()
        run = connection.execute(
            text(
                """
                SELECT contract_version, repository_id, workspace_id, base_commit,
                       workspace_path, status, stage
                FROM execution_runs WHERE id = 'legacy-contract-run'
                """
            )
        ).one()
        workspace_count = connection.execute(
            text("SELECT count(*) FROM task_workspaces")
        ).scalar_one()

    assert revision == "20260916_0008"
    assert task_repository is None
    assert tuple(run) == (1, None, None, None, None, "queued", "dispatch_pending")
    assert workspace_count == 0


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


def test_production_application_detects_repository_identity_index_drift(tmp_path):
    database_path = tmp_path / "repository-index-drift.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    migrated = _run_schema_cli(database_url, "migrate")
    assert migrated.returncode == 0, migrated.stderr

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ux_repositories_remote_identity")

    started = _run_production_startup(database_url)
    assert started.returncode != 0
    assert "schema drift detected" in started.stderr
    assert "repositories" in started.stderr
    assert "indexes" in started.stderr
