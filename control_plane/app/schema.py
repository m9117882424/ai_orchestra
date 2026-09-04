from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

from .db import Base
import app.models  # noqa: F401


CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]


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


def legacy_schema_diff(engine: Engine) -> list[str]:
    """Compare an unversioned database with the ORM baseline before stamping it.

    The comparison intentionally ignores SQL defaults because current model defaults
    are application-side. It compares table/column shape, nullability, primary keys,
    foreign keys and explicit indexes. Any ambiguity fails closed.
    """
    differences: list[str] = []
    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names()) - {"alembic_version"}
    expected_tables = set(Base.metadata.tables)

    for missing in sorted(expected_tables - actual_tables):
        differences.append(f"missing table: {missing}")
    for extra in sorted(actual_tables - expected_tables):
        differences.append(f"unexpected table: {extra}")
    if differences:
        return differences

    dialect = engine.dialect
    for table_name in sorted(expected_tables):
        table = Base.metadata.tables[table_name]
        expected_columns = {column.name: column for column in table.columns}
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
