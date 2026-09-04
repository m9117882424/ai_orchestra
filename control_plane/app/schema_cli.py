from __future__ import annotations

import argparse
import sys

from alembic import command
from alembic.migration import MigrationContext
from sqlalchemy import text

from .database_engine import create_configured_engine
from .schema import alembic_config, head_revision, legacy_schema_diff


LOCK_KEY = "ai_orchestra_schema_migration_v1"
engine = create_configured_engine()


def _current_revision(connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _run_under_lock(action) -> None:
    # engine.begin commits only after action returns. PostgreSQL transaction-level
    # advisory lock therefore remains held until the migration transaction is durably
    # committed or rolled back; another migrator cannot observe an intermediate state.
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": LOCK_KEY},
            )
        cfg = alembic_config()
        cfg.attributes["connection"] = connection
        action(connection, cfg)


def check_schema() -> None:
    expected = head_revision()
    with engine.connect() as connection:
        current = _current_revision(connection)
        if current != expected:
            raise RuntimeError(
                f"schema mismatch: current={current or 'unversioned'}, expected={expected}"
            )
        differences = legacy_schema_diff(connection)
    if differences:
        joined = "\n - ".join(differences)
        raise RuntimeError("schema shape mismatch despite valid revision:\n - " + joined)
    print(f"[OK] Control Plane schema revision and shape: {current}")


def migrate_schema() -> None:
    expected = head_revision()

    def migrate(connection, cfg) -> None:
        current = _current_revision(connection)
        if current == expected:
            differences = legacy_schema_diff(connection)
            if differences:
                joined = "\n - ".join(differences)
                raise RuntimeError("schema drift at current head:\n - " + joined)
            print(f"[OK] Schema already at head: {current}")
            return

        table_names = set(connection.dialect.get_table_names(connection)) - {"alembic_version"}
        if current is None and table_names:
            differences = legacy_schema_diff(connection)
            if differences:
                joined = "\n - ".join(differences)
                raise RuntimeError(
                    "Legacy database does not match the declared baseline; refusing stamp:\n - "
                    + joined
                )
            command.stamp(cfg, expected)
            final = _current_revision(connection)
            if final != expected:
                raise RuntimeError(f"stamp finished at {final}, expected {expected}")
            print(f"[OK] Existing schema verified and stamped at {expected}; data unchanged")
            return

        command.upgrade(cfg, "head")
        final = _current_revision(connection)
        if final != expected:
            raise RuntimeError(f"migration finished at {final}, expected {expected}")
        differences = legacy_schema_diff(connection)
        if differences:
            joined = "\n - ".join(differences)
            raise RuntimeError("migration reached head but schema shape differs:\n - " + joined)
        print(f"[OK] Schema migrated to {expected}")

    _run_under_lock(migrate)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Orchestra Control Plane schema manager")
    parser.add_argument("command", choices=("check", "migrate"))
    args = parser.parse_args()
    try:
        if args.command == "check":
            check_schema()
        else:
            migrate_schema()
    except Exception as exc:  # CLI boundary: print one deterministic failure and exit non-zero.
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
