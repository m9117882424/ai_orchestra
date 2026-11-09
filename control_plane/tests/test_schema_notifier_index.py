"""Focused regressions; run directly with unittest to avoid application fixtures.

Real PostgreSQL cases are opt-in: SCHEMA_TEST_PG_BIN=/path/to/postgresql/bin
python -m unittest control_plane.tests.test_schema_notifier_index -v
They create their own temporary cluster, use only a private Unix socket, disable
TCP, and never accept a database URL. No existing database is accessed.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import UniqueConstraint, create_engine, inspect
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app import schema
from control_plane.app.database_base import Base


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

    def test_sorting_and_constraint_backed_reserved_name(self):
        for variant in (
            {"column_sorting": {"created_at": ("desc",)}},
            {"column_sorting": {"id": ("nulls_first",)}},
            {"duplicates_constraint": schema._NOTIFIER_INDEX, "unique": True},
        ):
            with self.subTest(variant=variant):
                inspector = inspect(self.engine)
                original = inspector.get_indexes
                inspector.get_indexes = lambda table: ([{
                    "name": schema._NOTIFIER_INDEX,
                    "column_names": ["created_at", "id"], "unique": False, **variant,
                }] if table == "audit_events" else original(table))
                with patch.object(schema, "inspect", return_value=inspector):
                    self.assertTrue(schema.legacy_schema_diff(self.engine))

    def test_unknown_unique_constraint_and_legitimate_constraint(self):
        table = Base.metadata.tables["audit_events"]
        constraint = UniqueConstraint(table.c.action, name="test_action_unique")
        try:
            inspector = inspect(self.engine)
            get_uniques, get_indexes = inspector.get_unique_constraints, inspector.get_indexes
            inspector.get_unique_constraints = lambda name: ([{
                "name": "test_action_unique", "column_names": ["action"],
            }] if name == "audit_events" else get_uniques(name))
            inspector.get_indexes = lambda name: ([{
                "name": "test_action_unique", "column_names": ["action"], "unique": True,
                "duplicates_constraint": "test_action_unique",
            }] if name == "audit_events" else get_indexes(name))
            with patch.object(schema, "inspect", return_value=inspector):
                self.assertEqual(schema.legacy_schema_diff(self.engine), [])
                table.constraints.remove(constraint)
                self.assertIn("audit_events: unique constraints differ",
                              schema.legacy_schema_diff(self.engine))
        finally:
            table.constraints.discard(constraint)

    def test_duplicate_modeled_index(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE INDEX extra_task_id ON execution_runs(task_id)")
        self.assertTrue(schema.legacy_schema_diff(self.engine))

    def test_catalog_failure_is_closed_and_parameters_are_fixed(self):
        for value in (False, None, True):
            connection = Mock()
            connection.execute.return_value.scalar_one_or_none.return_value = value
            self.assertIs(schema._postgres_notifier_index_is_exact(connection), value is True)
            connection.execute.assert_called_once_with(schema._NOTIFIER_INDEX_SQL, {
                "schema": "public", "table_name": "audit_events",
                "index_name": schema._NOTIFIER_INDEX,
            })
        connection = Mock()
        connection.execute.side_effect = SQLAlchemyError("unavailable")
        self.assertFalse(schema._postgres_notifier_index_is_exact(connection))


@unittest.skipUnless(os.environ.get("SCHEMA_TEST_PG_BIN"), "isolated PostgreSQL opt-in not set")
class IsolatedPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_bin = Path(os.environ["SCHEMA_TEST_PG_BIN"])
        cls.tmp = tempfile.TemporaryDirectory(prefix="schema-index-pg-")
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        data = root / "data"
        def run(*args):
            subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        run(str(pg_bin / "initdb"), "-D", str(data), "-A", "trust", "--no-locale", "-U", "schema_test")
        try:
            run(str(pg_bin / "pg_ctl"), "-D", str(data), "-l", str(root / "server.log"),
                "-o", f"-c listen_addresses='' -c unix_socket_directories='{root}'", "-w", "start")
        except subprocess.CalledProcessError as error:
            raise RuntimeError((root / "server.log").read_text()) from error
        cls.addClassCleanup(run, str(pg_bin / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop")
        cls.engine = create_engine(URL.create("postgresql+psycopg", username="schema_test",
                                             database="postgres", query={"host": str(root)}))
        cls.addClassCleanup(cls.engine.dispose)
        Base.metadata.create_all(cls.engine)

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.addCleanup(self.connection.close)
        self.addCleanup(self.transaction.rollback)

    def create_index(self, keys="created_at, id", suffix="", unique=""):
        self.connection.exec_driver_sql(
            f"CREATE {unique} INDEX {schema._NOTIFIER_INDEX} ON audit_events ({keys}) {suffix}"
        )

    def test_absent_and_exact_with_unrelated_drift(self):
        self.assertEqual(schema.legacy_schema_diff(self.connection), [])
        self.create_index()
        self.assertTrue(schema._postgres_notifier_index_is_exact(self.connection))
        self.assertEqual(schema.legacy_schema_diff(self.connection), [])
        self.connection.exec_driver_sql("CREATE INDEX extra_task_id ON execution_runs(task_id)")
        self.assertTrue(schema.legacy_schema_diff(self.connection))

    def test_malformed_definitions(self):
        for keys, suffix, unique in (
            ("created_at DESC, id", "", ""),
            ("created_at NULLS FIRST, id", "", ""),
            ("created_at, id DESC NULLS LAST", "", ""),
            ("id, created_at", "", ""),
            ("created_at, id", "", "UNIQUE"),
            ("created_at, id", "WHERE action IS NOT NULL", ""),
            ("created_at, id", "INCLUDE (action)", ""),
            ("created_at, id varchar_pattern_ops", "", ""),
            ('created_at, id COLLATE "C"', "", ""),
            ("created_at, lower(id)", "", ""),
        ):
            with self.subTest(keys=keys, suffix=suffix, unique=unique):
                with self.connection.begin_nested() as savepoint:
                    self.create_index(keys, suffix, unique)
                    self.assertFalse(schema._postgres_notifier_index_is_exact(self.connection))
                    self.assertTrue(schema.legacy_schema_diff(self.connection))
                    savepoint.rollback()

    def test_nondefault_column_collation_and_access_method(self):
        self.connection.exec_driver_sql('ALTER TABLE audit_events ALTER COLUMN id TYPE varchar(36) COLLATE "C"')
        self.create_index()
        self.assertFalse(schema._postgres_notifier_index_is_exact(self.connection))
        self.assertTrue(schema.legacy_schema_diff(self.connection))
        self.connection.exec_driver_sql(f"DROP INDEX {schema._NOTIFIER_INDEX}")
        self.connection.exec_driver_sql(
            f"CREATE INDEX {schema._NOTIFIER_INDEX} ON audit_events USING brin(created_at, id)"
        )
        self.assertFalse(schema._postgres_notifier_index_is_exact(self.connection))
        self.assertTrue(schema.legacy_schema_diff(self.connection))

    def test_constraint_backed_indexes(self):
        for name, keys in ((schema._NOTIFIER_INDEX, "created_at, id"), ("extra_action", "action")):
            with self.subTest(name=name):
                with self.connection.begin_nested() as savepoint:
                    self.connection.exec_driver_sql(f"ALTER TABLE audit_events ADD CONSTRAINT {name} UNIQUE ({keys})")
                    self.assertTrue(schema.legacy_schema_diff(self.connection))
                    savepoint.rollback()

    def test_catalog_validity_and_readiness(self):
        self.create_index()
        for flag in ("indisvalid", "indisready", "indislive"):
            with self.subTest(flag=flag):
                with self.connection.begin_nested() as savepoint:
                    # Only this disposable cluster's catalog is changed, never a supplied DB.
                    self.connection.exec_driver_sql(
                        f"UPDATE pg_catalog.pg_index SET {flag} = false "
                        f"WHERE indexrelid = '{schema._NOTIFIER_INDEX}'::regclass"
                    )
                    self.assertFalse(schema._postgres_notifier_index_is_exact(self.connection))
                    self.assertTrue(schema.legacy_schema_diff(self.connection))
                    savepoint.rollback()

    def test_wrong_schema_even_with_valid_public_decoy(self):
        self.create_index()
        self.connection.exec_driver_sql("CREATE SCHEMA shadow")
        self.connection.exec_driver_sql("CREATE TABLE shadow.audit_events (LIKE public.audit_events INCLUDING ALL)")
        self.connection.exec_driver_sql(
            f"CREATE INDEX {schema._NOTIFIER_INDEX} ON shadow.audit_events(created_at, id)"
        )
        self.connection.exec_driver_sql("SET LOCAL search_path = shadow, public")
        self.assertFalse(schema._postgres_notifier_index_is_exact(self.connection))
        self.assertTrue(schema.legacy_schema_diff(self.connection))


if __name__ == "__main__":
    unittest.main()
