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
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import time
from urllib import error, request
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    "execution.timed_out": ("🔴", "Превышено время выполнения"),
    "execution.stalled_tool": ("🔴", "Зависание инструмента исполнителя"),
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
try:
    TIMEZONE = ZoneInfo("Europe/Istanbul")
except ZoneInfoNotFoundError:
    # Slim images may not ship zoneinfo files; Turkey uses UTC+03:00 in 2026.
    TIMEZONE = timezone(timedelta(hours=3))
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
    reconciliation_interval_seconds: int = 3600
    reconciliation_window_days: int = 7


def _read_secret(env_name: str) -> str:
    path = os.environ.get(env_name, "")
    if not path.startswith("/"):
        raise ValueError(f"{env_name} must reference an absolute secret file")
    secret_path = Path(path)
    if ".." in secret_path.parts:
        raise ValueError(f"{env_name} contains an unsafe path segment")
    # O_NOFOLLOW guards the final target; lstat guards intermediate symlinks.
    for component in (secret_path, *secret_path.parents):
        if component == Path("/"):
            break
        component_stat = os.lstat(component)
        if stat.S_ISLNK(component_stat.st_mode):
            raise ValueError(f"{env_name} must not traverse symlinks")
        if component == secret_path and not stat.S_ISREG(component_stat.st_mode):
            raise ValueError(f"{env_name} must be a regular secret file")
    # Even if the final path changes from a regular file to a FIFO after lstat,
    # O_NONBLOCK prevents startup from hanging before fstat can reject it.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o077
            or info.st_size > 8192
        ):
            raise ValueError(f"{env_name} must be root-owned, private and regular")
        # Avoid printing secret content or token-bearing paths in errors.
        result = os.read(fd, 8192).decode("utf-8").strip()
    finally:
        os.close(fd)
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
        self.lock_file = path.with_name(path.name + ".lock").open("a+b")
        os.chmod(self.lock_file.name, 0o600)
        # Multiple workers with the same state volume must never deliver a
        # single event concurrently. flock is released on process death.
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError("Notifier already has an active state owner") from None
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

    def bootstrap(self, server_now: datetime) -> bool:
        """Create/recover cursors atomically; never reset an older starting point.

        Returns True only when this is a genuinely fresh installation.
        """
        existing_start = self.get_time("started_at")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                ("started_at", _utc(server_now).isoformat()),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                ("scan_through", _utc(existing_start or server_now).isoformat()),
            )
            if existing_start is None:
                self.db.execute(
                    "INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                    ("last_reconciled_at", _utc(server_now).isoformat()),
                )
        return existing_start is None

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
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
        self.lock_file.close()


class AuditReader:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @staticmethod
    def _read_only_transaction(conn) -> None:
        # Defense in depth: an accidentally overprivileged connection must
        # never be able to mutate orchestration state through this client.
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '2000ms'")
        conn.execute("SET LOCAL lock_timeout = '500ms'")

    def current_time(self) -> datetime:
        import psycopg  # Imported only by runtime, not pure unit tests.

        with psycopg.connect(self.dsn, connect_timeout=7) as conn:
            self._read_only_transaction(conn)
            # Must be a non-admin, audit-only reader. Enumerate the effective
            # privileges of *all* application relations, including TRUNCATE,
            # rather than checking only the tasks table.
            role = conn.execute(
                "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls, "
                "EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=pg_roles.oid) "
                "FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            if role is None or any(role) or not conn.execute(
                "SELECT current_user = session_user"
            ).fetchone()[0]:
                raise PermissionError("Notifier role has administrative privileges")
            if not conn.execute(
                "SELECT has_table_privilege(current_user,'public.audit_events','SELECT')"
            ).fetchone()[0]:
                raise PermissionError("Notifier cannot read the audit log")
            elevated = conn.execute(
                "SELECT COALESCE(bool_or("
                "has_table_privilege(current_user,c.oid,'INSERT') OR "
                "has_any_column_privilege(current_user,c.oid,'INSERT') OR "
                "has_table_privilege(current_user,c.oid,'UPDATE') OR "
                "has_any_column_privilege(current_user,c.oid,'UPDATE') OR "
                "has_table_privilege(current_user,c.oid,'DELETE') OR "
                "has_table_privilege(current_user,c.oid,'TRUNCATE') OR "
                "has_table_privilege(current_user,c.oid,'REFERENCES') OR "
                "has_any_column_privilege(current_user,c.oid,'REFERENCES') OR "
                "has_table_privilege(current_user,c.oid,'TRIGGER') OR "
                "(c.oid <> 'public.audit_events'::regclass AND "
                "(has_table_privilege(current_user,c.oid,'SELECT') OR "
                "has_any_column_privilege(current_user,c.oid,'SELECT')))), FALSE) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname<>'information_schema' AND left(n.nspname,3)<>'pg_' "
                "AND c.relkind IN ('r','p','v','m','f')"
            ).fetchone()[0]
            sequences = conn.execute(
                "SELECT COALESCE(bool_or("
                "has_sequence_privilege(current_user,c.oid,'USAGE') OR "
                "has_sequence_privilege(current_user,c.oid,'SELECT') OR "
                "has_sequence_privilege(current_user,c.oid,'UPDATE')), FALSE) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname<>'information_schema' AND left(n.nspname,3)<>'pg_' "
                "AND c.relkind='S'"
            ).fetchone()[0]
            can_create = conn.execute(
                "SELECT has_database_privilege(current_user,current_database(),'CREATE') OR "
                "EXISTS (SELECT 1 FROM pg_namespace n "
                "WHERE n.nspname<>'information_schema' AND left(n.nspname,3)<>'pg_' "
                "AND has_schema_privilege(current_user,n.oid,'CREATE'))"
            ).fetchone()[0]
            if elevated or sequences or can_create:
                raise PermissionError("Notifier requires a narrow, audit-only database role")
            index_ready = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_index idx "
                "JOIN pg_class i ON i.oid=idx.indexrelid "
                "JOIN pg_am am ON am.oid=i.relam "
                "JOIN pg_class t ON t.oid=idx.indrelid "
                "JOIN pg_namespace n ON n.oid=t.relnamespace "
                "JOIN pg_attribute created ON created.attrelid=t.oid AND created.attname='created_at' "
                "JOIN pg_attribute event_id ON event_id.attrelid=t.oid AND event_id.attname='id' "
                "JOIN pg_opclass op0 ON op0.oid=idx.indclass[0] "
                "JOIN pg_opclass op1 ON op1.oid=idx.indclass[1] "
                "WHERE n.nspname='public' AND t.relname='audit_events' "
                "AND i.relname='ix_ai_orchestra_audit_events_created_id' "
                "AND am.amname='btree' "
                "AND idx.indisvalid AND idx.indisready AND idx.indpred IS NULL "
                "AND idx.indexprs IS NULL AND idx.indnkeyatts >= 2 "
                "AND idx.indoption[0]=0 AND idx.indoption[1]=0 "
                "AND idx.indcollation[0]=created.attcollation "
                "AND idx.indcollation[1]=event_id.attcollation "
                "AND op0.opcdefault AND op1.opcdefault "
                "AND op0.opcintype=created.atttypid "
                "AND (op1.opcintype=event_id.atttypid OR "
                "(event_id.atttypid='pg_catalog.varchar'::regtype AND "
                "op1.opcintype='pg_catalog.text'::regtype)) "
                "AND idx.indkey[0]=created.attnum AND idx.indkey[1]=event_id.attnum)"
            ).fetchone()[0]
            if not index_ready:
                raise RuntimeError("Notifier requires the audit (created_at,id) index")
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
            self._read_only_transaction(conn)
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


class TelegramRateLimited(RuntimeError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = (
            retry_after_seconds
            if isinstance(retry_after_seconds, int) and retry_after_seconds > 0
            else 60
        )
        super().__init__("Telegram rate limited delivery")


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
            if exc.code == 429:
                retry_after = 60
                try:
                    body = json.loads(exc.read(4096).decode("utf-8"))
                    candidate = body.get("parameters", {}).get("retry_after")
                    if isinstance(candidate, int) and not isinstance(candidate, bool):
                        retry_after = candidate
                except (ValueError, UnicodeError, AttributeError, TypeError):
                    pass
                raise TelegramRateLimited(retry_after) from None
            raise RuntimeError(f"Telegram HTTP {exc.code}") from None
        except error.URLError:
            raise RuntimeError("Telegram transport unavailable") from None


def poll_once(settings: Settings, state: StateStore, reader: AuditReader, sender: TelegramSender) -> int:
    server_now = _utc(reader.current_time())
    if state.bootstrap(server_now):
        # Both cursor keys committed in one SQLite transaction.
        LOG.info("Initial cursor set; no historical audit events will be replayed")
        return 0

    backoff_until = state.get_time("telegram_retry_not_before")
    if backoff_until is not None and server_now < _utc(backoff_until):
        return 0

    until = server_now - timedelta(seconds=settings.settle_seconds)
    started_at = state.get_time("started_at")
    scanned_at = state.get_time("scan_through")
    assert started_at is not None and scanned_at is not None
    if until <= scanned_at:
        return 0
    last_reconciled = state.get_time("last_reconciled_at")
    needs_reconciliation = (
        last_reconciled is None
        or server_now - _utc(last_reconciled) >= timedelta(seconds=settings.reconciliation_interval_seconds)
    )
    def deliver_range(since: datetime) -> int:
        after: tuple[datetime, str] | None = None
        count = 0
        while True:
            events = reader.page(since, until, after, settings.batch_size)
            for event in events:
                if event.action not in ALLOWED_ACTIONS or state.was_delivered(event.id):
                    continue
                try:
                    sender.send(format_notice(event))
                except TelegramRateLimited as exc:
                    now = max(server_now, datetime.now(timezone.utc))
                    try:
                        not_before = now + timedelta(seconds=exc.retry_after_seconds)
                    except OverflowError:
                        not_before = datetime.max.replace(tzinfo=timezone.utc)
                    state.set_time("telegram_retry_not_before", not_before)
                    raise
                state.mark_delivered(event.id, server_now)
                count += 1
            if len(events) < settings.batch_size:
                break
            last = events[-1]
            after = (last.created_at, last.id)
        return count

    # Backlog scan is independent of the bounded reconciliation pass. A server
    # can be offline longer than the late-commit window: never lose the oldest
    # ordinary undelivered events by replacing this scan with the 7-day sweep.
    delivered = deliver_range(max(started_at, scanned_at - timedelta(seconds=settings.overlap_seconds)))
    state.set_time("scan_through", until)
    if needs_reconciliation:
        # The independent retrospective pass detects late commits; delivery
        # dedup is durable so rescanning cannot resend confirmed messages.
        since = max(started_at, until - timedelta(days=settings.reconciliation_window_days))
        delivered += deliver_range(since)
        state.set_time("last_reconciled_at", server_now)
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
