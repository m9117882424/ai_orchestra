from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from notifications.telegram_notifier import (
    ALLOWED_ACTIONS,
    AuditNotice,
    MessageLimiter,
    Settings,
    StateStore,
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

    def send(self, text):
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

    def test_limiter_blocks_eleventh_message_in_minute(self):
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


if __name__ == "__main__":
    unittest.main()
