"""Exact CHECK reflection contract on disposable PostgreSQL 14 and 16 schemas.

Run with POSTGRES_CHECK_TEST_URL pointing only at a disposable test database.
The CI matrix supplies this URL; without it the runtime test is explicitly skipped.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, create_engine, inspect, text
from sqlalchemy.engine import make_url

from control_plane.app.database_base import Base
import control_plane.app.models  # noqa: F401
from control_plane.app.schema import (
    CODEX_BACKEND_REVISION,
    _POSTGRES_BACKEND_CHECK_0012,
    alembic_config,
    legacy_schema_diff,
    schema_diff_for_revision,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "app" / "check_constraints_postgresql.json"
BACKEND = "ck_execution_runs_backend"


@pytest.fixture
def pg_schema():
    raw_url = os.environ.get("POSTGRES_CHECK_TEST_URL")
    if not raw_url:
        pytest.skip("UNVERIFIED locally: POSTGRES_CHECK_TEST_URL is unset")
    url = make_url(raw_url)
    if (url.drivername != "postgresql+psycopg" or url.database != "check_matrix"
            or url.host not in ("127.0.0.1", "localhost")):
        pytest.fail("POSTGRES_CHECK_TEST_URL must target local postgresql+psycopg/check_matrix")
    admin = create_engine(url)
    with admin.connect() as connection:
        major = connection.dialect.server_version_info[0]
        assert major in (14, 16), f"unsupported PostgreSQL {major}"
    name = "check_matrix_" + uuid4().hex
    # The random name is generated here; the configured database is CI-only.
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{name}"')
    scoped_url = url.update_query_dict({"options": f"-csearch_path={name}"})
    engine = create_engine(scoped_url)
    try:
        yield engine, scoped_url.render_as_string(hide_password=False), major
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{name}" CASCADE')
        admin.dispose()


def _checks(engine):
    inspector = inspect(engine)
    return {
        f"{table}.{row['name']}": row
        for table in Base.metadata.tables
        for row in inspector.get_check_constraints(table)
    }


def _assert_manifest(actual, committed, label):
    actual_text = json.dumps(actual, indent=2, sort_keys=True).splitlines()
    committed_text = json.dumps(committed, indent=2, sort_keys=True).splitlines()
    difference = "\n".join(difflib.unified_diff(
        committed_text, actual_text, fromfile="committed manifest",
        tofile=label, lineterm="",
    ))
    assert not difference, difference


def _upgrade(engine, revision):
    cfg = alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)


def _cli(url, action):
    env = os.environ.copy()
    env.update(CONTROL_PLANE_DATABASE_URL=url, CONTROL_PLANE_ENVIRONMENT="test")
    return subprocess.run(
        [sys.executable, "-m", "app.schema_cli", action], cwd=ROOT,
        env=env, text=True, capture_output=True, check=False,
    )


def _backend(engine):
    return next(row for row in inspect(engine).get_check_constraints("execution_runs")
                if row["name"] == BACKEND)


def _replace_backend(engine, expression, *, suffix=""):
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"ALTER TABLE execution_runs DROP CONSTRAINT {BACKEND}"
        )
        connection.exec_driver_sql(
            f"ALTER TABLE execution_runs ADD CONSTRAINT {BACKEND} "
            f"CHECK ({expression}) {suffix}"
        )


def test_manifest_matches_orm_and_alembic_inspector_sqltext(pg_schema):
    engine, _, major = pg_schema
    from importlib.metadata import version
    assert version("SQLAlchemy") == "2.0.52"
    committed = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected_names = {
        f"{table.name}.{constraint.name}"
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }
    assert set(committed) == expected_names

    Base.metadata.create_all(engine)
    orm = _checks(engine)
    assert set(orm) == expected_names
    _assert_manifest({key: row["sqltext"] for key, row in orm.items()},
                     committed, f"PostgreSQL {major} ORM Inspector")
    assert all(not row.get("dialect_options") for row in orm.values())
    assert legacy_schema_diff(engine) == []

    Base.metadata.drop_all(engine)
    _upgrade(engine, "head")
    migrated = _checks(engine)
    assert set(migrated) == expected_names
    _assert_manifest({key: row["sqltext"] for key, row in migrated.items()},
                     committed, f"PostgreSQL {major} Alembic Inspector")
    assert all(not row.get("dialect_options") for row in migrated.values())
    assert legacy_schema_diff(engine) == []


def test_0012_versioned_and_unversioned_never_stamp_head_shape(pg_schema):
    engine, url, _ = pg_schema
    _upgrade(engine, CODEX_BACKEND_REVISION)
    assert _backend(engine)["sqltext"] == _POSTGRES_BACKEND_CHECK_0012
    assert schema_diff_for_revision(engine, CODEX_BACKEND_REVISION) == []
    assert legacy_schema_diff(engine)
    result = _cli(url, "migrate")
    assert result.returncode == 0, result.stderr
    assert "Schema migrated" in result.stdout
    assert "opencode-review" in _backend(engine)["sqltext"]

    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")
    # An unversioned 0013 schema may be stamped only after exact shape checks.
    result = _cli(url, "migrate")
    assert result.returncode == 0, result.stderr
    assert "stamped at 20261107_0013" in result.stdout


def test_unversioned_0012_upgrades_and_malformed_check_is_refused(pg_schema):
    engine, url, _ = pg_schema
    _upgrade(engine, CODEX_BACKEND_REVISION)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")
    assert legacy_schema_diff(engine)
    assert schema_diff_for_revision(engine, CODEX_BACKEND_REVISION) == []
    result = _cli(url, "migrate")
    assert result.returncode == 0, result.stderr
    assert "Historical baseline 20261010_0012 verified" in result.stdout
    assert "opencode-review" in _backend(engine)["sqltext"]
    _replace_backend(engine, "true")
    assert legacy_schema_diff(engine)
    result = _cli(url, "migrate")
    assert result.returncode != 0
    assert "schema drift at current head" in result.stderr


def test_malformed_versioned_0012_refuses_upgrade_and_stamp(pg_schema):
    engine, url, _ = pg_schema
    _upgrade(engine, CODEX_BACKEND_REVISION)
    _replace_backend(engine, "true")
    assert schema_diff_for_revision(engine, CODEX_BACKEND_REVISION)
    result = _cli(url, "migrate")
    assert result.returncode != 0
    assert "refusing upgrade" in result.stderr
    with engine.begin() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == CODEX_BACKEND_REVISION
        connection.exec_driver_sql("DROP TABLE alembic_version")
    result = _cli(url, "migrate")
    assert result.returncode != 0
    assert "refusing stamp" in result.stderr
    assert "alembic_version" not in inspect(engine).get_table_names()


@pytest.mark.parametrize("suffix", ["NOT VALID", "NO INHERIT"])
def test_reflected_check_options_are_refused(pg_schema, suffix):
    engine, url, _ = pg_schema
    _upgrade(engine, "head")
    _replace_backend(
        engine, "backend IN ('opencode', 'codex-subscription', 'opencode-review')",
        suffix=suffix,
    )
    actual = _backend(engine)
    assert actual.get("dialect_options"), actual
    assert legacy_schema_diff(engine)
    result = _cli(url, "migrate")
    assert result.returncode != 0
    assert "schema drift at current head" in result.stderr


def test_stale_versioned_0013_and_0012_shape_cannot_be_stamped(pg_schema):
    engine, url, _ = pg_schema
    _upgrade(engine, "head")
    _replace_backend(engine, "backend IN ('opencode', 'codex-subscription')")
    assert legacy_schema_diff(engine)
    result = _cli(url, "migrate")
    assert result.returncode != 0
    assert "schema drift at current head" in result.stderr
    with engine.begin() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261107_0013"
        connection.exec_driver_sql("DROP TABLE alembic_version")
    result = _cli(url, "migrate")
    assert result.returncode == 0, result.stderr
    assert "Historical baseline 20261010_0012 verified" in result.stdout
    assert "opencode-review" in _backend(engine)["sqltext"]
