from __future__ import annotations

import argparse
import sys

from alembic import command
from alembic.migration import MigrationContext
from sqlalchemy import text

from .db import engine
from .schema import alembic_config, head_revision, legacy_schema_diff


LOCK_KEY = "ai_orchestra_schema_migration_v1"


def _current_revision(connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _run_under_lock(action) -> None:
    with engine.begin() as connection:
        is_postgres = engine.dialect.name == "postgresql"
        if is_postgres:
            connection.execute(text("SELECT pg_advisory_lock(hashtext(:key))"), {"key": LOCK_KEY})
        try:
            cfg = alembic_config()
            cfg.attributes["connection"] = connection
            action(connection, cfg)
        finally:
            if is_postgres:
                connection.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": LOCK_KEY})


def check_schema() -> None:
    expected = head_revision()
    with engine.connect() as connection:
        current = _current_revision(connection)
    if current != expected:
        raise RuntimeError(
            f"schema mismatch: current={current or 'unversioned'}, expected={expected}"
        )
    print(f"[OK] Control Plane schema revision: {current}")


def migrate_schema() -> None:
    expected = head_revision()

    def migrate(connection, cfg) -> None:
        current = _current_revision(connection)
        if current == expected:
            print(f"[OK] Schema already at head: {current}")
            return

        table_names = set(connection.dialect.get_table_names(connection)) - {"alembic_version"}
        if current is None and table_names:
            differences = legacy_schema_diff(engine)
            if differences:
                joined = "\n - ".join(differences)
                raise RuntimeError(
                    "Legacy database does not match the declared baseline; refusing stamp:\n - "
                    + joined
                )
            command.stamp(cfg, expected)
            print(f"[OK] Existing schema verified and stamped at {expected}; data unchanged")
            return

        command.upgrade(cfg, "head")
        final = _current_revision(connection)
        if final != expected:
            raise RuntimeError(f"migration finished at {final}, expected {expected}")
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
