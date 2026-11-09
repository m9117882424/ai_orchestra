from __future__ import annotations

from collections import Counter
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Connection, Engine

from .database_base import Base
from . import models as _models  # noqa: F401


CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
LEGACY_BASELINE_REVISION = "20260904_0001"
PREVIOUS_HEAD_REVISION = "20260917_0011"

_REVISION_EXCLUDED_COLUMNS = {
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


_NOTIFIER_INDEX = "ix_ai_orchestra_audit_events_created_id"

# Reflection omits validity, readiness and collation. Resolve the same unqualified
# table as Inspector, then require that it and its index actually belong to public.
_NOTIFIER_INDEX_SQL = text("""
SELECT i.indisvalid AND i.indisready AND i.indislive
    AND NOT i.indisunique AND NOT i.indisprimary AND NOT i.indisexclusion
    AND tn.nspname = :schema AND xn.nspname = :schema
    AND am.amname = 'btree' AND i.indnkeyatts = 2 AND i.indnatts = 2
    AND i.indexprs IS NULL AND i.indpred IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conindid = x.oid
    )
    AND (
        SELECT count(*) = 2
        FROM pg_catalog.generate_series(0, 1) AS key_position(n)
        JOIN pg_catalog.pg_attribute a
          ON a.attrelid = t.oid AND a.attnum = i.indkey[n]
        JOIN pg_catalog.pg_opclass opc ON opc.oid = i.indclass[n]
        WHERE a.attname = CASE n WHEN 0 THEN 'created_at' ELSE 'id' END
          AND NOT a.attisdropped AND i.indoption[n] = 0
          AND i.indcollation[n] = a.attcollation
          AND i.indcollation[n] = CASE n WHEN 0 THEN 0::oid
              ELSE 'pg_catalog."default"'::regcollation::oid END
          AND opc.opcdefault AND opc.opcmethod = am.oid
          AND opc.opcnamespace = 'pg_catalog'::regnamespace
          AND opc.opcname = CASE n WHEN 0 THEN 'timestamptz_ops' ELSE 'text_ops' END
    ) AS exact_shape
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
JOIN pg_catalog.pg_namespace tn ON tn.oid = t.relnamespace
JOIN pg_catalog.pg_class x ON x.oid = i.indexrelid
JOIN pg_catalog.pg_namespace xn ON xn.oid = x.relnamespace
JOIN pg_catalog.pg_am am ON am.oid = x.relam
WHERE t.oid = pg_catalog.to_regclass(:table_name) AND x.relname = :index_name
""")


# A reserved relation can be the backing index of a primary-key constraint.
# PostgreSQL's Inspector.get_indexes() deliberately omits primary-key indexes,
# so the reserved name must be checked independently of reflected indexes.
_NOTIFIER_RESERVED_NAME_COUNT_SQL = text("""
SELECT count(*)
FROM pg_catalog.pg_class
WHERE relname = :index_name
""")


def _postgres_reserved_notifier_name_is_safe(bind: Engine | Connection) -> bool:
    try:
        if isinstance(bind, Engine):
            with bind.connect() as connection:
                return _postgres_reserved_notifier_name_is_safe(connection)
        count = bind.execute(
            _NOTIFIER_RESERVED_NAME_COUNT_SQL, {"index_name": _NOTIFIER_INDEX}
        ).scalar_one()
        if count == 0:
            return True  # Notifier has not been enabled; the index is optional.
        return count == 1 and _postgres_notifier_index_is_exact(bind)
    except SQLAlchemyError:
        # Fail closed if catalog inspection is unavailable.
        return False


def _postgres_notifier_index_is_exact(bind: Engine | Connection) -> bool:
    try:
        if isinstance(bind, Engine):
            with bind.connect() as connection:
                return _postgres_notifier_index_is_exact(connection)
        return bind.execute(
            _NOTIFIER_INDEX_SQL,
            {"schema": "public", "table_name": "audit_events", "index_name": _NOTIFIER_INDEX},
        ).scalar_one_or_none() is True
    except SQLAlchemyError:
        # Missing catalog visibility or unsupported catalog layout cannot prove
        # the exception safe. Do not expose database error details here.
        return False


def _schema_diff(
    bind: Engine | Connection,
    *,
    expected_absent_tables: frozenset[str] = frozenset(),
    excluded_columns: frozenset[tuple[str, str]] = frozenset(),
    excluded_indexes: frozenset[tuple[str, tuple[str, ...], bool]] = frozenset(),
    excluded_checks: frozenset[tuple[str, str]] = frozenset(),
    nullable_overrides: dict[tuple[str, str], bool] | None = None,
) -> list[str]:
    """Compare a database with an exact declared schema shape.

    Historical exclusions/overrides reconstruct only explicitly known Alembic
    baselines from today's ORM metadata. Partial or unknown drift still fails closed.
    """
    if isinstance(bind, Engine):
        # Reflection and catalog verification must use the same search_path/session.
        with bind.connect() as connection:
            return _schema_diff(
                connection,
                expected_absent_tables=expected_absent_tables,
                excluded_columns=excluded_columns,
                excluded_indexes=excluded_indexes,
                excluded_checks=excluded_checks,
                nullable_overrides=nullable_overrides,
            )
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
    if dialect.name == "postgresql" and not _postgres_reserved_notifier_name_is_safe(bind):
        differences.append("audit_events: indexes: invalid reserved notifier catalog relation")
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
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
            and (table_name, constraint.name) not in excluded_checks
        }
        actual_checks = {
            constraint.get("name")
            for constraint in inspector.get_check_constraints(table_name)
            if constraint.get("name")
        }
        if actual_checks != expected_checks:
            differences.append(
                f"{table_name}: check constraints {sorted(actual_checks)} "
                f"!= {sorted(expected_checks)}"
            )

        expected_uniques = Counter(
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
            and not any((table_name, column.name) in excluded_columns
                        for column in constraint.columns)
        )
        unique_constraints = inspector.get_unique_constraints(table_name)
        actual_uniques = Counter(
            tuple(constraint.get("column_names") or []) for constraint in unique_constraints
        )
        if actual_uniques != expected_uniques:
            differences.append(f"{table_name}: unique constraints differ")
        constraint_names = {constraint.get("name") for constraint in unique_constraints}

        expected_indexes = Counter(
            (tuple(column.name for column in index.columns), bool(index.unique))
            for index in table.indexes
            if (table_name, tuple(column.name for column in index.columns), bool(index.unique))
            not in excluded_indexes
        )
        actual_indexes = Counter()
        for index in inspector.get_indexes(table_name):
            columns = tuple(index.get("column_names") or [])
            unique = bool(index.get("unique"))
            # G5 notifier owns this *one* externally-managed read index.
            # Do not weaken the schema guard for any other index, columns,
            # uniqueness or a filtered/functional variant with this name.
            opts = index.get("dialect_options") or {}
            plain = not any(
                value not in (None, "", (), [])
                for value in opts.values()
            )
            reserved = index.get("name") == _NOTIFIER_INDEX
            if (
                table_name == "audit_events"
                and reserved
                and columns == ("created_at", "id")
                and not unique
                and plain
                and not index.get("column_sorting")
                and not index.get("include_columns")
                and not index.get("expressions")
                and not index.get("duplicates_constraint")
                and (dialect.name != "postgresql" or _postgres_notifier_index_is_exact(bind))
            ):
                continue
            if reserved:
                differences.append(f"{table_name}: indexes: invalid reserved notifier index")
            elif (index.get("duplicates_constraint")
                  and index["duplicates_constraint"] in constraint_names):
                # Its shape was checked above; do not double-count its backing index.
                continue
            actual_indexes[(columns, unique)] += 1
        if actual_indexes != expected_indexes:
            differences.append(
                f"{table_name}: indexes {sorted(actual_indexes.items(), key=repr)} "
                f"!= {sorted(expected_indexes.items(), key=repr)}"
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
    )


def assert_database_shape(bind: Engine | Connection) -> None:
    differences = legacy_schema_diff(bind)
    if differences:
        joined = "\n - ".join(differences)
        raise RuntimeError("Control Plane schema drift detected:\n - " + joined)
