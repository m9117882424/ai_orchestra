from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib import error
from uuid import uuid4

from notifications.telegram_notifier import (
    ALLOWED_ACTIONS,
    AuditNotice,
    AuditReader,
    MessageLimiter,
    Settings,
    StateStore,
    TelegramRateLimited,
    TelegramSender,
    _read_secret,
    format_notice,
    poll_once,
)


UTC = timezone.utc


def notice(action: str, when: datetime, entity: str | None = None) -> AuditNotice:
    return AuditNotice(
        id=str(uuid4()),
        action=action,
        entity_type="task",
        entity_id=entity or str(uuid4()),
        created_at=when,
    )


class FakeReader:
    def __init__(self, now: datetime, events: list[AuditNotice]):
        self.now = now
        self.events = events

    def current_time(self) -> datetime:
        return self.now

    def page(self, since, until, after, size):
        rows = sorted(
            (e for e in self.events if since <= e.created_at <= until and e.action in ALLOWED_ACTIONS),
            key=lambda e: (e.created_at, e.id),
        )
        if after is not None:
            rows = [e for e in rows if (e.created_at, e.id) > after]
        return rows[:size]


class FakeSender:
    def __init__(self):
        self.messages = []
        self.fail_next = False
        self.rate_limit_next = 0

    def send(self, text):
        if self.rate_limit_next:
            wait = self.rate_limit_next
            self.rate_limit_next = 0
            raise TelegramRateLimited(wait)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("network failure with potential secret")
        self.messages.append(text)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "private" / "state.sqlite3"
        self.state = StateStore(self.state_path)
        self.now = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)
        self.cfg = Settings(db_dsn="not used", token="not used", chat_id="42707740", state_path=self.state_path,
                            settle_seconds=5, overlap_seconds=120, batch_size=2)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_bootstrap_skips_history_and_delivers_only_new_events(self):
        old = notice("task.created", self.now - timedelta(days=2))
        newer = notice("execution.completed", self.now + timedelta(seconds=15))
        reader = FakeReader(self.now, [old, newer])
        sender = FakeSender()
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)
        reader.now = self.now + timedelta(seconds=25)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 1)
        self.assertEqual(len(sender.messages), 1)
        self.assertIn("Запуск успешно завершён", sender.messages[0])
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)

    def test_restarts_preserve_dedup_and_cursor(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        poll_once(self.cfg, self.state, reader, sender)
        reader.events = [notice("task.status_changed", self.now + timedelta(seconds=7))]
        reader.now = self.now + timedelta(seconds=20)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 1)
        self.state.close()
        self.state = StateStore(self.state_path)
        reader.now += timedelta(seconds=20)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)
        self.assertEqual(len(sender.messages), 1)

    def test_send_failure_keeps_checkpoint_for_retry(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        poll_once(self.cfg, self.state, reader, sender)
        event = notice("approval.requested", self.now + timedelta(seconds=3))
        reader.events = [event]
        reader.now += timedelta(seconds=20)
        sender.fail_next = True
        previous = self.state.get_time("scan_through")
        with self.assertRaises(RuntimeError):
            poll_once(self.cfg, self.state, reader, sender)
        self.assertEqual(self.state.get_time("scan_through"), previous)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 1)
        self.assertTrue(self.state.was_delivered(event.id))

    def test_keyset_pages_include_events_with_same_timestamp(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        poll_once(self.cfg, self.state, reader, sender)
        identical = self.now + timedelta(seconds=10)
        reader.events = [notice("task.created", identical) for _ in range(5)]
        reader.now = self.now + timedelta(seconds=25)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 5)
        self.assertEqual(len(sender.messages), 5)

    def test_internal_events_are_not_forwarded(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        poll_once(self.cfg, self.state, reader, sender)
        reader.events = [notice("repository.sync_failed", self.now + timedelta(seconds=8))]
        reader.now = self.now + timedelta(seconds=20)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)
        self.assertFalse(sender.messages)

    def test_format_notice_contains_no_untrusted_details(self):
        event = AuditNotice(
            id=str(uuid4()), action="task.created", entity_type="email@example.com",
            entity_id="customer sensitive name", created_at=self.now,
        )
        rendered = format_notice(event)
        self.assertNotIn("email@example.com", rendered)
        self.assertNotIn("customer sensitive name", rendered)
        self.assertIn("AI Orchestra", rendered)
        self.assertIn("Стамбул", rendered)

    def test_limiter_blocks_excess_messages_in_minute(self):
        fake_time = [100.0]
        waits = []

        def monotonic():
            return fake_time[0]

        def sleep(seconds):
            waits.append(seconds)
            fake_time[0] += seconds

        limiter = MessageLimiter(2, clock=monotonic, sleep=sleep)
        limiter.wait_turn()
        limiter.wait_turn()
        limiter.wait_turn()
        self.assertEqual(waits, [60.0])

    def test_second_worker_cannot_share_active_state(self):
        with self.assertRaisesRegex(RuntimeError, "active state owner"):
            StateStore(self.state_path)

    def test_bootstrap_is_atomic_when_sqlite_aborts_between_cursor_keys(self):
        self.state.db.execute(
            "CREATE TRIGGER simulate_crash BEFORE INSERT ON meta "
            "WHEN NEW.key='scan_through' BEGIN SELECT RAISE(ABORT,'simulated crash'); END"
        )
        with self.assertRaisesRegex(Exception, "simulated crash"):
            self.state.bootstrap(self.now)
        self.assertIsNone(self.state.get_time("started_at"))
        self.assertIsNone(self.state.get_time("scan_through"))
        self.state.db.execute("DROP TRIGGER simulate_crash")
        self.assertTrue(self.state.bootstrap(self.now))
        self.assertEqual(self.state.get_time("started_at"), self.now)
        self.assertEqual(self.state.get_time("scan_through"), self.now)

    def test_recover_old_started_at_with_missing_scan_cursor(self):
        earlier = self.now - timedelta(hours=5)
        self.state.set_time("started_at", earlier)
        reader = FakeReader(self.now, [notice("task.created", earlier + timedelta(minutes=3))])
        sender = FakeSender()
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 1)
        self.assertEqual(self.state.get_time("started_at"), earlier)
        self.assertEqual(len(sender.messages), 1)

    def test_late_commit_older_than_overlap_is_found_by_reconciliation(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)
        reader.now = self.now + timedelta(minutes=12)
        poll_once(self.cfg, self.state, reader, sender)
        event = notice("task.created", self.now + timedelta(seconds=15))
        reader.events = [event]  # committed > 120 s after its timestamp
        reader.now = self.now + timedelta(minutes=72)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 1)
        self.assertTrue(self.state.was_delivered(event.id))
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)

    def test_long_outage_delivers_all_backlog_older_than_reconciliation_window(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)
        event = notice("task.created", self.now + timedelta(days=1))
        reader.events = [event]
        reader.now = self.now + timedelta(days=12)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 1)
        self.assertTrue(self.state.was_delivered(event.id))
        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)

    def test_terminal_timeout_and_stalled_tool_events_are_delivered(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        poll_once(self.cfg, self.state, reader, sender)
        reader.events = [
            notice("execution.timed_out", self.now + timedelta(seconds=1)),
            notice("execution.stalled_tool", self.now + timedelta(seconds=2)),
        ]
        reader.now += timedelta(seconds=30)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 2)
        self.assertTrue(any("Превышено время" in m for m in sender.messages))
        self.assertTrue(any("Зависание инструмента" in m for m in sender.messages))

    def test_429_persisted_not_before_survives_restart(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        poll_once(self.cfg, self.state, reader, sender)
        event = notice("task.created", self.now + timedelta(seconds=10))
        reader.events = [event]
        reader.now += timedelta(seconds=25)
        sender.rate_limit_next = 300
        with self.assertRaises(TelegramRateLimited):
            poll_once(self.cfg, self.state, reader, sender)
        not_before = self.state.get_time("telegram_retry_not_before")
        self.assertIsNotNone(not_before)
        self.assertGreaterEqual(not_before, reader.now + timedelta(seconds=300))
        self.state.close()
        self.state = StateStore(self.state_path)
        reader.now = not_before - timedelta(seconds=1)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 0)
        self.assertEqual(len(sender.messages), 0)
        reader.now = not_before + timedelta(seconds=6)
        self.assertEqual(poll_once(self.cfg, self.state, reader, sender), 1)
        self.assertTrue(self.state.was_delivered(event.id))

    def test_telegram_http_429_parses_retry_after_without_leaking_token(self):
        sender = TelegramSender("123456:secret_token_testing_only", "42707740", MessageLimiter(10))
        http_error = error.HTTPError(
            "https://api.telegram.org/bot123456:secret_token_testing_only/sendMessage",
            429,
            "Too Many Requests",
            None,
            BytesIO(b'{"ok":false,"parameters":{"retry_after":73}}'),
        )
        with mock.patch("notifications.telegram_notifier.request.urlopen", side_effect=http_error):
            with self.assertRaises(TelegramRateLimited) as raised:
                sender.send("safe")
        self.assertEqual(raised.exception.retry_after_seconds, 73)
        self.assertNotIn("secret_token", str(raised.exception))

    def test_429_long_retry_after_is_not_truncated(self):
        sender = TelegramSender("123456:secret_token_testing_only", "42707740", MessageLimiter(10))
        http_error = error.HTTPError(
            "https://api.telegram.org/bot123456:secret_token_testing_only/sendMessage",
            429, "Too Many Requests", None,
            BytesIO(b'{"ok":false,"parameters":{"retry_after":7200}}'),
        )
        with mock.patch("notifications.telegram_notifier.request.urlopen", side_effect=http_error):
            with self.assertRaises(TelegramRateLimited) as raised:
                sender.send("safe")
        self.assertEqual(raised.exception.retry_after_seconds, 7200)

    def test_unrepresentable_retry_after_pauses_until_max_timestamp(self):
        reader = FakeReader(self.now, [])
        sender = FakeSender()
        poll_once(self.cfg, self.state, reader, sender)
        reader.events = [notice("task.created", self.now + timedelta(seconds=1))]
        reader.now += timedelta(seconds=10)
        sender.rate_limit_next = 10**30
        with self.assertRaises(TelegramRateLimited):
            poll_once(self.cfg, self.state, reader, sender)
        self.assertEqual(self.state.get_time("telegram_retry_not_before").year, 9999)

    def test_read_secret_rejects_public_permissions_symlinks_and_non_root(self):
        secret = Path(self.temp.name) / "private-secret"
        secret.write_text("TEST_SECRET", encoding="utf-8")
        os.chmod(secret, 0o644)
        with mock.patch.dict(os.environ, {"TEST_SECRET_FILE": str(secret)}):
            with self.assertRaisesRegex(ValueError, "private"):
                _read_secret("TEST_SECRET_FILE")
            os.chmod(secret, 0o600)
            original_fstat = os.fstat

            def nonroot_fstat(fd):
                info = original_fstat(fd)
                return SimpleNamespace(st_mode=info.st_mode, st_uid=1234, st_size=info.st_size)

            with mock.patch("notifications.telegram_notifier.os.fstat", side_effect=nonroot_fstat):
                with self.assertRaisesRegex(ValueError, "root-owned"):
                    _read_secret("TEST_SECRET_FILE")
            # Synthetic root owner lets this pass on both root and CI runners.
            def root_fstat(fd):
                info = original_fstat(fd)
                return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_size=info.st_size)

            with mock.patch("notifications.telegram_notifier.os.fstat", side_effect=root_fstat):
                self.assertEqual(_read_secret("TEST_SECRET_FILE"), "TEST_SECRET")
            symlink = Path(self.temp.name) / "symlink-secret"
            symlink.symlink_to(secret)
            os.environ["TEST_SECRET_FILE"] = str(symlink)
            with self.assertRaisesRegex(ValueError, "symlink"):
                _read_secret("TEST_SECRET_FILE")
            self.assertTrue(stat.S_ISLNK(symlink.lstat().st_mode))

    def test_fifo_secret_rejected_without_blocking(self):
        fifo = Path(self.temp.name) / "fifo-secret"
        os.mkfifo(fifo, mode=0o600)
        snippet = (
            "import os; "
            "from notifications.telegram_notifier import _read_secret; "
            "_read_secret('TEST_SECRET_FILE')"
        )
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            env={**os.environ, "TEST_SECRET_FILE": str(fifo)},
            capture_output=True, text=True, timeout=2, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular secret file", result.stderr)

    def test_postgres_reader_forces_read_only_and_checks_all_privileges_and_index(self):
        class FakeResult:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return self.value

            def fetchall(self):
                return self.value

        class FakePg:
            def __init__(self):
                self.sql = []
                self.extra_grants = False
                self.has_index = True

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                self.sql.append((sql, params))
                if sql.startswith("SET "):
                    return FakeResult(None)
                if sql.startswith("SELECT rolsuper"):
                    return FakeResult((False,) * 6)
                if "SELECT current_user = session_user" in sql:
                    return FakeResult((True,))
                if sql.startswith("SELECT has_table_privilege(current_user,'public.audit_events'"):
                    return FakeResult((True,))
                if "relkind IN" in sql:
                    return FakeResult((self.extra_grants,))
                if "relkind='S'" in sql or "has_schema_privilege" in sql:
                    return FakeResult((False,))
                if "SELECT EXISTS" in sql:
                    return FakeResult((self.has_index,))
                if "clock_timestamp" in sql:
                    return FakeResult((self_now,))
                if sql.startswith("SELECT id,action,entity_type,entity_id,created_at"):
                    return FakeResult([])
                raise AssertionError(f"unexpected SQL: {sql[:100]}")

        self_now = self.now
        pg = FakePg()
        with mock.patch.dict(sys.modules, {"psycopg": SimpleNamespace(connect=lambda *_a, **_kw: pg)}):
            reader = AuditReader("redacted")
            self.assertEqual(reader.current_time(), self.now)
            self.assertEqual(reader.page(self.now, self.now, None, 3), [])
            self.assertEqual([sql for sql, _ in pg.sql if sql.startswith("SET TRANSACTION")],
                             ["SET TRANSACTION READ ONLY", "SET TRANSACTION READ ONLY"])
            self.assertTrue(any("TRUNCATE" in sql and "REFERENCES" in sql for sql, _ in pg.sql))
            self.assertTrue(any("has_any_column_privilege" in sql and "left(n.nspname,3)" in sql
                                for sql, _ in pg.sql))
            self.assertTrue(any("statement_timeout" in sql for sql, _ in pg.sql))
            self.assertTrue(any("lock_timeout" in sql for sql, _ in pg.sql))
            self.assertTrue(any("am.amname='btree'" in sql and "opcdefault" in sql
                                and "idx.indoption[0]=0" in sql for sql, _ in pg.sql))
            pg.extra_grants = True
            with self.assertRaises(PermissionError):
                reader.current_time()
            pg.extra_grants = False
            pg.has_index = False
            with self.assertRaisesRegex(RuntimeError, "index"):
                reader.current_time()


if __name__ == "__main__":
    unittest.main()
