from __future__ import annotations

from importlib.metadata import version as package_version
import json
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, inspect
from sqlalchemy.engine import Connection, Engine

from .database_base import Base
from . import models as _models  # noqa: F401


CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
LEGACY_BASELINE_REVISION = "20260904_0001"
PREVIOUS_HEAD_REVISION = "20260917_0011"
CODEX_BACKEND_REVISION = "20261010_0012"
_BACKEND_CHECK = ("execution_runs", "ck_execution_runs_backend")
_BACKEND_CHECK_0012 = "backend IN ('opencode', 'codex-subscription')"
# SQLAlchemy Inspector.get_check_constraints() sqltext, verified against ORM
# and Alembic schemas by tests/test_postgres_check_matrix.py on PostgreSQL 14/16.
# This is deliberately exact text: no expression normalization is permitted.
_POSTGRES_CHECKS = json.loads(
    (Path(__file__).with_name("check_constraints_postgresql.json")).read_text(encoding="utf-8")
)
_POSTGRES_BACKEND_CHECK_0012 = (
    "backend::text = ANY (ARRAY['opencode'::character varying, "
    "'codex-subscription'::character varying]::text[])"
)

_REVISION_EXCLUDED_COLUMNS = {
    CODEX_BACKEND_REVISION: frozenset(),
    PREVIOUS_HEAD_REVISION: frozenset({("execution_runs", "backend")}),
    LEGACY_BASELINE_REVISION: frozenset(
        {
            ("execution_runs", "backend"),
            ("execution_runs", "lease_owner"),
            ("execution_runs", "lease_generation"),
            ("execution_runs", "heartbeat_at"),
            ("execution_runs", "lease_expires_at"),
            ("execution_runs", "deadline_at"),
            ("execution_runs", "cancel_requested_at"),
            ("execution_runs", "contract_version"),
            ("execution_runs", "repository_id"),
            ("execution_runs", "workspace_id"),
            ("execution_runs", "base_commit"),
            ("execution_runs", "workspace_path"),
            ("execution_runs", "workspace_tree"),
            ("execution_runs", "workspace_preflight_digest"),
            ("execution_runs", "workspace_preflight_completed_at"),
            ("execution_runs", "workspace_runtime_preflight_digest"),
            ("execution_runs", "workspace_runtime_verified_at"),
            ("tasks", "repository_id"),
            ("usage_events", "execution_id"),
            ("usage_events", "source"),
            ("usage_events", "source_key"),
            ("usage_events", "child_run_id"),
        }
    )
}
_REVISION_ABSENT_TABLES = {
    LEGACY_BASELINE_REVISION: frozenset(
        {
            "repositories",
            "task_workspaces",
            "runner_jobs",
            "execution_evidence",
            "execution_result_packages",
            "execution_child_runs",
        }
    ),
}
_REVISION_EXCLUDED_INDEXES = {
    CODEX_BACKEND_REVISION: frozenset(),
    PREVIOUS_HEAD_REVISION: frozenset(),
    LEGACY_BASELINE_REVISION: frozenset(
        {
            ("execution_runs", ("lease_expires_at",), False),
            ("execution_runs", ("status", "deadline_at"), False),
            ("execution_runs", ("repository_id",), False),
            ("execution_runs", ("workspace_id",), True),
            ("tasks", ("repository_id",), False),
            ("usage_events", ("execution_id",), False),
            ("usage_events", ("child_run_id",), False),
            ("usage_events", ("execution_id", "source", "source_key"), True),
        }
    )
}
_REVISION_EXCLUDED_CHECKS = {
    CODEX_BACKEND_REVISION: frozenset(),
    PREVIOUS_HEAD_REVISION: frozenset({("execution_runs", "ck_execution_runs_backend")}),
    LEGACY_BASELINE_REVISION: frozenset(
        {
            ("execution_runs", "ck_execution_runs_backend"),
            ("execution_runs", "ck_execution_runs_contract_version"),
            ("execution_runs", "ck_execution_runs_workspace_binding"),
            ("execution_runs", "ck_execution_runs_workspace_preflight"),
            ("execution_runs", "ck_execution_runs_workspace_runtime_preflight"),
            ("usage_events", "ck_usage_events_source_pair"),
        }
    )
}
_REVISION_NULLABLE_OVERRIDES = {
    # Revision 0001 required an OpenCode session before ExecutionRun persistence.
    # Head 0003 intentionally makes it nullable so queued dispatch can be durable.
    LEGACY_BASELINE_REVISION: {
        ("execution_runs", "opencode_session_id"): False,
    }
}


def alembic_config() -> Config:
    cfg = Config(str(CONTROL_PLANE_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(CONTROL_PLANE_ROOT / "migrations"))
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if not head:
        raise RuntimeError("Alembic head revision не определена")
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def assert_database_at_head(engine: Engine) -> None:
    expected = head_revision()
    current = current_revision(engine)
    if current != expected:
        raise RuntimeError(
            "Схема Control Plane не готова: "
            f"current={current or 'unversioned'}, expected={expected}. "
            "Сначала выполните управляемую миграцию."
        )


def _compiled_type(column_type, dialect) -> str:
    return " ".join(column_type.compile(dialect=dialect).lower().split())


def _expected_check_sql(table_name: str, constraint: CheckConstraint, dialect_name: str,
                        revision: str | None) -> str:
    key = (table_name, constraint.name)
    if dialect_name == "sqlite":
        if revision == CODEX_BACKEND_REVISION and key == _BACKEND_CHECK:
            return _BACKEND_CHECK_0012
        return str(constraint.sqltext)
    if dialect_name == "postgresql":
        if revision == CODEX_BACKEND_REVISION and key == _BACKEND_CHECK:
            return _POSTGRES_BACKEND_CHECK_0012
        # PostgreSQL deparses IN, casts, and parentheses differently from the
        # model source. These are the exact PostgreSQL-rendered head expressions.
        return _POSTGRES_CHECKS[f"{table_name}.{constraint.name}"]
    raise RuntimeError(f"Unsupported dialect for CHECK verification: {dialect_name}")


def _schema_diff(
    bind: Engine | Connection,
    *,
    expected_absent_tables: frozenset[str] = frozenset(),
    excluded_columns: frozenset[tuple[str, str]] = frozenset(),
    excluded_indexes: frozenset[tuple[str, tuple[str, ...], bool]] = frozenset(),
    excluded_checks: frozenset[tuple[str, str]] = frozenset(),
    nullable_overrides: dict[tuple[str, str], bool] | None = None,
    revision: str | None = None,
) -> list[str]:
    """Compare a database with an exact declared schema shape.

    Historical exclusions/overrides reconstruct only explicitly known Alembic
    baselines from today's ORM metadata. Partial or unknown drift still fails closed.
    """
    differences: list[str] = []
    nullable_overrides = nullable_overrides or {}
    inspector = inspect(bind)
    actual_tables = set(inspector.get_table_names()) - {"alembic_version"}
    expected_tables = set(Base.metadata.tables) - expected_absent_tables

    for missing in sorted(expected_tables - actual_tables):
        differences.append(f"missing table: {missing}")
    for extra in sorted(actual_tables - expected_tables):
        differences.append(f"unexpected table: {extra}")
    if differences:
        return differences

    dialect = bind.dialect
    if dialect.name == "postgresql":
        server_major = dialect.server_version_info[0]
        if server_major not in (14, 16):
            raise RuntimeError(f"Unsupported PostgreSQL CHECK rendering: {server_major}")
        if package_version("SQLAlchemy") != "2.0.52":
            raise RuntimeError("PostgreSQL CHECK verification requires SQLAlchemy 2.0.52")
        declared_checks = {
            f"{name}.{constraint.name}"
            for name in expected_tables
            for constraint in Base.metadata.tables[name].constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        all_declared_checks = {
            f"{name}.{constraint.name}"
            for name, table in Base.metadata.tables.items()
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        if set(_POSTGRES_CHECKS) != all_declared_checks or not declared_checks <= set(_POSTGRES_CHECKS):
            raise RuntimeError("PostgreSQL CHECK manifest differs from ORM model names")
    for table_name in sorted(expected_tables):
        table = Base.metadata.tables[table_name]
        expected_columns = {
            column.name: column
            for column in table.columns
            if (table_name, column.name) not in excluded_columns
        }
        actual_columns = {column["name"]: column for column in inspector.get_columns(table_name)}

        for missing in sorted(set(expected_columns) - set(actual_columns)):
            differences.append(f"{table_name}: missing column {missing}")
        for extra in sorted(set(actual_columns) - set(expected_columns)):
            differences.append(f"{table_name}: unexpected column {extra}")

        for column_name in sorted(set(expected_columns) & set(actual_columns)):
            expected = expected_columns[column_name]
            actual = actual_columns[column_name]
            expected_type = _compiled_type(expected.type, dialect)
            actual_type = _compiled_type(actual["type"], dialect)
            if expected_type != actual_type:
                differences.append(
                    f"{table_name}.{column_name}: type {actual_type!r} != {expected_type!r}"
                )
            expected_nullable = nullable_overrides.get(
                (table_name, column_name), bool(expected.nullable)
            )
            if bool(actual["nullable"]) != expected_nullable:
                differences.append(
                    f"{table_name}.{column_name}: nullable={actual['nullable']} "
                    f"!= {expected_nullable}"
                )

        expected_pk = tuple(column.name for column in table.primary_key.columns)
        actual_pk = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
        if set(actual_pk) != set(expected_pk):
            differences.append(f"{table_name}: primary key {actual_pk} != {expected_pk}")

        expected_fks = {
            (
                tuple(fk.parent.name for fk in constraint.elements),
                constraint.referred_table.name,
                tuple(fk.column.name for fk in constraint.elements),
                (constraint.ondelete or "").upper(),
            )
            for constraint in table.foreign_key_constraints
            if constraint.referred_table.name not in expected_absent_tables
            and not any(
                (table_name, element.parent.name) in excluded_columns
                for element in constraint.elements
            )
        }
        actual_fks = {
            (
                tuple(fk.get("constrained_columns") or []),
                fk.get("referred_table"),
                tuple(fk.get("referred_columns") or []),
                str((fk.get("options") or {}).get("ondelete") or "").upper(),
            )
            for fk in inspector.get_foreign_keys(table_name)
        }
        if actual_fks != expected_fks:
            differences.append(f"{table_name}: foreign keys differ")

        expected_checks = {
            constraint.name: constraint
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
            and (table_name, constraint.name) not in excluded_checks
        }
        actual_check_rows = inspector.get_check_constraints(table_name)
        actual_checks = {
            constraint.get("name"): constraint
            for constraint in actual_check_rows
            if constraint.get("name")
        }
        if actual_checks.keys() != expected_checks.keys():
            differences.append(
                f"{table_name}: check constraints {sorted(actual_checks)} "
                f"!= {sorted(expected_checks)}"
            )
        if len(actual_check_rows) != len(actual_checks):
            differences.append(f"{table_name}: unnamed or duplicate CHECK constraint")
        for name in sorted(expected_checks.keys() & actual_checks.keys()):
            expected_sql = _expected_check_sql(
                table_name, expected_checks[name], dialect.name, revision
            )
            actual = actual_checks[name]
            if (actual.get("sqltext") != expected_sql
                    or actual.get("dialect_options")):
                differences.append(f"{table_name}: CHECK {name} expression differs")

        expected_indexes = {
            (tuple(column.name for column in index.columns), bool(index.unique))
            for index in table.indexes
            if (table_name, tuple(column.name for column in index.columns), bool(index.unique))
            not in excluded_indexes
        }
        actual_indexes = {
            (tuple(index.get("column_names") or []), bool(index.get("unique")))
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        }
        if actual_indexes != expected_indexes:
            differences.append(
                f"{table_name}: indexes {sorted(actual_indexes)} != {sorted(expected_indexes)}"
            )

    return differences


def legacy_schema_diff(bind: Engine | Connection) -> list[str]:
    """Compare a database with the current declared ORM head exactly."""
    return _schema_diff(bind)


def schema_diff_for_revision(bind: Engine | Connection, revision: str) -> list[str]:
    """Compare an unversioned database with one explicitly supported old baseline."""
    if revision not in _REVISION_EXCLUDED_COLUMNS:
        raise RuntimeError(f"Unsupported historical schema revision: {revision}")
    return _schema_diff(
        bind,
        expected_absent_tables=_REVISION_ABSENT_TABLES.get(revision, frozenset()),
        excluded_columns=_REVISION_EXCLUDED_COLUMNS[revision],
        excluded_indexes=_REVISION_EXCLUDED_INDEXES[revision],
        excluded_checks=_REVISION_EXCLUDED_CHECKS.get(revision, frozenset()),
        nullable_overrides=_REVISION_NULLABLE_OVERRIDES.get(revision),
        revision=revision,
    )


def assert_database_shape(bind: Engine | Connection) -> None:
    differences = legacy_schema_diff(bind)
    if differences:
        joined = "\n - ".join(differences)
        raise RuntimeError("Control Plane schema drift detected:\n - " + joined)
