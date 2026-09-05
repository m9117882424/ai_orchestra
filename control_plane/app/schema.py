from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from .database_base import Base
from . import models as _models  # noqa: F401


CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
LEGACY_BASELINE_REVISION = "20260904_0001"

_REVISION_EXCLUDED_COLUMNS = {
    LEGACY_BASELINE_REVISION: frozenset(
        {
            ("execution_runs", "lease_owner"),
            ("execution_runs", "lease_generation"),
            ("execution_runs", "heartbeat_at"),
            ("execution_runs", "lease_expires_at"),
        }
    )
}
_REVISION_EXCLUDED_INDEXES = {
    LEGACY_BASELINE_REVISION: frozenset(
        {
            ("execution_runs", ("lease_expires_at",), False),
        }
    )
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


def _schema_diff(
    bind: Engine | Connection,
    *,
    excluded_columns: frozenset[tuple[str, str]] = frozenset(),
    excluded_indexes: frozenset[tuple[str, tuple[str, ...], bool]] = frozenset(),
) -> list[str]:
    """Compare a database with an exact declared schema shape.

    Exclusions are used only to reconstruct a specifically known historical Alembic
    baseline from today's ORM metadata. Any partially migrated/unknown shape still
    fails closed because extra columns or indexes remain visible as unexpected.
    """
    differences: list[str] = []
    inspector = inspect(bind)
    actual_tables = set(inspector.get_table_names()) - {"alembic_version"}
    expected_tables = set(Base.metadata.tables)

    for missing in sorted(expected_tables - actual_tables):
        differences.append(f"missing table: {missing}")
    for extra in sorted(actual_tables - expected_tables):
        differences.append(f"unexpected table: {extra}")
    if differences:
        return differences

    dialect = bind.dialect
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
            if bool(actual["nullable"]) != bool(expected.nullable):
                differences.append(
                    f"{table_name}.{column_name}: nullable={actual['nullable']} "
                    f"!= {expected.nullable}"
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
        excluded_columns=_REVISION_EXCLUDED_COLUMNS[revision],
        excluded_indexes=_REVISION_EXCLUDED_INDEXES[revision],
    )


def assert_database_shape(bind: Engine | Connection) -> None:
    differences = legacy_schema_diff(bind)
    if differences:
        joined = "\n - ".join(differences)
        raise RuntimeError("Control Plane schema drift detected:\n - " + joined)
