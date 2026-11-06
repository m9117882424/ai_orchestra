"""Read-only PostgreSQL audit tail -> Telegram, with durable delivery tracking.

The notifier never reads audit details, task titles, or customer data. It only
reads the allowlisted event metadata from the *main* Control Plane database.
It is deliberately opt-in and has no manager/worker API credentials.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import ssl
import time
from urllib import error, request
from uuid import UUID
from zoneinfo import ZoneInfo


LOG = logging.getLogger("ai_orchestra.telegram_notifier")
TELEGRAM_ENDPOINT = "https://api.telegram.org/bot{token}/sendMessage"
ALLOWED_ACTIONS = {
    "task.created": ("🗂", "Создана задача"),
    "task.status_changed": ("🔄", "Изменён статус задачи"),
    "task.repository_changed": ("🔗", "Задача связана с репозиторием"),
    "approval.requested": ("🟠", "Требуется согласование"),
    "approval.approved": ("✅", "Согласование получено"),
    "approval.rejected": ("⛔", "Согласование отклонено"),
    "execution.queued": ("⏳", "Запуск поставлен в очередь"),
    "execution.completed": ("✅", "Запуск успешно завершён"),
    "execution.failed": ("🔴", "Запуск завершился ошибкой"),
    "execution.cancelled": ("⏹", "Запуск отменён"),
    "execution.opencode_error": ("🔴", "Ошибка запуска"),
    "execution.runner_gate_failed": ("🔴", "Не пройдена проверка запуска"),
    "runner_job.failed": ("🔴", "Ошибка задания исполнителя"),
}
ENTITY_NAMES = {
    "task": "Задача",
    "approval": "Согласование",
    "execution": "Запуск",
    "execution_run": "Запуск",
    "runner_job": "Задание",
}
TIMEZONE = ZoneInfo("Europe/Istanbul")
TOKEN_PATTERN = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{20,}$")
CHAT_PATTERN = re.compile(r"^-?[0-9]{4,20}$")


@dataclass(frozen=True)
class AuditNotice:
    id: str
    action: str
    entity_type: str
    entity_id: str
    created_at: datetime


@dataclass(frozen=True)
class Settings:
    db_dsn: str
    token: str
    chat_id: str
    state_path: Path
    poll_seconds: int = 15
    batch_size: int = 100
    overlap_seconds: int = 120
    settle_seconds: int = 5
    max_messages_per_minute: int = 10


def _read_secret(env_name: str) -> str:
    path = os.environ.get(env_name, "")
    if not path.startswith("/"):
        raise ValueError(f"{env_name} must reference an absolute secret file")
    # Avoid printing secret content or resolving paths in error messages.
    result = Path(path).read_text(encoding="utf-8").strip()
    if not result:
        raise ValueError(f"{env_name} file is empty")
    return result


def load_settings() -> Settings:
    db_dsn = _read_secret("ORCHESTRA_NOTIFY_DB_DSN_FILE")
    token = _read_secret("ORCHESTRA_NOTIFY_TELEGRAM_TOKEN_FILE")
    chat_id = _read_secret("ORCHESTRA_NOTIFY_TELEGRAM_CHAT_ID_FILE")
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Telegram token file has an invalid format")
    if not CHAT_PATTERN.fullmatch(chat_id):
        raise ValueError("Telegram chat ID file has an invalid format")
    state_path = Path(os.environ.get("ORCHESTRA_NOTIFY_STATE_PATH", "/var/lib/orchestra-notifier/state.sqlite3"))
    if not state_path.is_absolute():
        raise ValueError("ORCHESTRA_NOTIFY_STATE_PATH must be absolute")
    return Settings(db_dsn=db_dsn, token=token, chat_id=chat_id, state_path=state_path)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("Audit timestamps must include a timezone")
    return dt.astimezone(timezone.utc)


def format_notice(event: AuditNotice) -> str:
    """Only constant labels, UUID prefixes and event time leave the server."""
    icon, label = ALLOWED_ACTIONS[event.action]
    kind = ENTITY_NAMES.get(event.entity_type, "Событие")
    try:
        identifier = str(UUID(event.entity_id))[:8]
    except (ValueError, TypeError, AttributeError):
        identifier = "—"
    timestamp = _utc(event.created_at).astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")
    return (
        f"{icon} AI Orchestra · {label}\n"
        f"{kind}: {identifier}\n"
        f"Время: {timestamp} (Стамбул)\n"
        "Кабинет: https://ai.equippulse.com/"
    )


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS delivered (event_id TEXT PRIMARY KEY, delivered_at TEXT NOT NULL)"
        )
        self.db.commit()

    def get_time(self, key: str) -> datetime | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def set_time(self, key: str, value: datetime) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _utc(value).isoformat()),
        )
        self.db.commit()

    def was_delivered(self, event_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM delivered WHERE event_id = ?", (event_id,)).fetchone()
        return row is not None

    def mark_delivered(self, event_id: str, now: datetime) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO delivered(event_id,delivered_at) VALUES (?,?)",
            (event_id, _utc(now).isoformat()),
        )
        self.db.commit()

    def prune(self, now: datetime) -> None:
        before = (_utc(now) - timedelta(days=90)).isoformat()
        self.db.execute("DELETE FROM delivered WHERE delivered_at < ?", (before,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


class AuditReader:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def current_time(self) -> datetime:
        import psycopg  # Imported only by runtime, not pure unit tests.

        with psycopg.connect(self.dsn, connect_timeout=7) as conn:
            # Connectivity alone is insufficient: this must be a role that can
            # read audit_events (and is granted no additional tables).
            conn.execute("SELECT id FROM public.audit_events LIMIT 0")
            grants = conn.execute(
                "SELECT "
                "has_table_privilege(current_user,'public.audit_events','SELECT'),"
                "has_table_privilege(current_user,'public.audit_events','INSERT'),"
                "has_table_privilege(current_user,'public.audit_events','UPDATE'),"
                "has_table_privilege(current_user,'public.audit_events','DELETE'),"
                "has_table_privilege(current_user,'public.tasks','SELECT')"
            ).fetchone()
            if grants != (True, False, False, False, False):
                raise PermissionError("Notifier requires a narrow, audit-only database role")
            return conn.execute("SELECT clock_timestamp()").fetchone()[0]

    def page(
        self,
        since: datetime,
        until: datetime,
        after: tuple[datetime, str] | None,
        size: int,
    ) -> list[AuditNotice]:
        import psycopg

        sql = (
            "SELECT id,action,entity_type,entity_id,created_at FROM public.audit_events "
            "WHERE created_at >= %s AND created_at <= %s AND action = ANY(%s)"
        )
        params: list = [since, until, list(ALLOWED_ACTIONS)]
        if after is not None:
            sql += " AND (created_at,id) > (%s,%s)"
            params.extend(after)
        sql += " ORDER BY created_at ASC,id ASC LIMIT %s"
        params.append(size)
        with psycopg.connect(self.dsn, connect_timeout=7) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [AuditNotice(*row) for row in rows]


class MessageLimiter:
    """Conservative, process-local cap; state-based event dedup survives restarts."""

    def __init__(self, max_per_minute: int, clock=time.monotonic, sleep=time.sleep):
        self.limit = max_per_minute
        self.clock = clock
        self.sleep = sleep
        self.sent: deque[float] = deque()

    def wait_turn(self) -> None:
        now = self.clock()
        while self.sent and now - self.sent[0] >= 60:
            self.sent.popleft()
        if len(self.sent) >= self.limit:
            self.sleep(max(0.01, 60 - (now - self.sent[0])))
            now = self.clock()
            while self.sent and now - self.sent[0] >= 60:
                self.sent.popleft()
        self.sent.append(now)


class TelegramSender:
    def __init__(self, token: str, chat_id: str, limiter: MessageLimiter):
        self._token = token
        self._chat_id = chat_id
        self.limiter = limiter

    def send(self, text: str) -> None:
        self.limiter.wait_turn()
        payload = json.dumps(
            {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
            ensure_ascii=False,
        ).encode("utf-8")
        req = request.Request(
            TELEGRAM_ENDPOINT.format(token=self._token),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
            data=payload,
        )
        try:
            with request.urlopen(req, timeout=10, context=ssl.create_default_context()) as response:
                result = json.load(response)
                if response.status != 200 or result.get("ok") is not True:
                    raise RuntimeError("Telegram API did not confirm delivery")
        except error.HTTPError as exc:
            # Never log exception strings: urllib errors can include token-bearing URL.
            raise RuntimeError(f"Telegram HTTP {exc.code}") from None
        except error.URLError:
            raise RuntimeError("Telegram transport unavailable") from None


def poll_once(settings: Settings, state: StateStore, reader: AuditReader, sender: TelegramSender) -> int:
    server_now = _utc(reader.current_time())
    if state.get_time("started_at") is None:
        # First start only: do not send any historical events.
        state.set_time("started_at", server_now)
        state.set_time("scan_through", server_now)
        LOG.info("Initial cursor set; no historical audit events will be replayed")
        return 0

    until = server_now - timedelta(seconds=settings.settle_seconds)
    started_at = state.get_time("started_at")
    scanned_at = state.get_time("scan_through")
    assert started_at is not None and scanned_at is not None
    if until <= scanned_at:
        return 0
    since = max(started_at, scanned_at - timedelta(seconds=settings.overlap_seconds))
    after: tuple[datetime, str] | None = None
    delivered = 0
    while True:
        events = reader.page(since, until, after, settings.batch_size)
        for event in events:
            if event.action not in ALLOWED_ACTIONS:
                continue
            if state.was_delivered(event.id):
                continue
            sender.send(format_notice(event))
            state.mark_delivered(event.id, server_now)
            delivered += 1
        if len(events) < settings.batch_size:
            break
        last = events[-1]
        after = (last.created_at, last.id)
    state.set_time("scan_through", until)
    state.prune(server_now)
    return delivered


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in AI Orchestra Telegram audit notifier")
    parser.add_argument("--check", action="store_true", help="Validate secrets and DB access without sending")
    parser.add_argument("--once", action="store_true", help="Perform one poll")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings()
        reader = AuditReader(settings.db_dsn)
        reader.current_time()
        if args.check:
            print("OK: bot configuration and read-only audit DB connection are available")
            return 0
        if os.environ.get("ORCHESTRA_NOTIFY_SEND_ENABLED") != "yes":
            raise ValueError("Delivery is disabled; set ORCHESTRA_NOTIFY_SEND_ENABLED=yes explicitly")
        state = StateStore(settings.state_path)
        sender = TelegramSender(
            settings.token, settings.chat_id,
            MessageLimiter(settings.max_messages_per_minute),
        )
        try:
            failed = False
            while True:
                try:
                    sent = poll_once(settings, state, reader, sender)
                    LOG.info("Poll completed; delivered=%s", sent)
                except Exception:
                    # Deliberately do not print exceptions: DSNs and URLs may carry secrets.
                    LOG.error("Notification poll failed; retrying without advancing cursor")
                    failed = True
                if args.once:
                    break
                time.sleep(settings.poll_seconds)
        finally:
            state.close()
        return 1 if failed else 0
    except Exception:
        # Never include exception text; driver errors may contain credential DSNs.
        LOG.error("Notifier startup failed; check configuration and access")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
