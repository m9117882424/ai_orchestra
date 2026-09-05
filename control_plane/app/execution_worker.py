from __future__ import annotations

import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .execution_protocol import (
    EXECUTION_METADATA_KEY,
    execution_message_id,
    execution_prompt,
    execution_session_title,
)
from .models import ExecutionRun, Task
from .opencode_client import OpenCodeClient, OpenCodeError, extract_last_assistant_text
from .schema import assert_database_shape
from .services import write_audit
from .settings import get_settings


LOGGER = logging.getLogger("ai_orchestra.execution_worker")
ACTIVE_EXECUTION_STATUSES = ("queued", "running")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_int(name: str, default: int, *, minimum: int = 1, maximum: int = 10_000) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class ExecutionLease:
    execution_id: str
    generation: int
    status: str
    opencode_session_id: str | None


class ExecutionLeaseManager:
    """PostgreSQL-backed lease and fencing boundary for dispatch and completion.

    Queued dispatch and running observation share the same lease generation. Any
    external side effect is reconciled before retry, while durable state may change
    only when the caller still owns the exact unexpired generation.
    """

    def __init__(self, worker_id: str, *, lease_seconds: int = 120):
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    @property
    def audit_actor(self) -> str:
        return f"execution-worker:{self.worker_id}"[:100]

    def _deadline(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.lease_seconds)

    def claim_available(
        self,
        db: Session,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> list[ExecutionLease]:
        if limit <= 0:
            return []
        now = now or utc_now()
        rows = list(
            db.scalars(
                select(ExecutionRun)
                .where(
                    ExecutionRun.status.in_(ACTIVE_EXECUTION_STATUSES),
                    or_(
                        ExecutionRun.lease_expires_at.is_(None),
                        ExecutionRun.lease_expires_at <= now,
                    ),
                )
                .order_by(ExecutionRun.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        leases: list[ExecutionLease] = []
        for run in rows:
            previous_generation = int(run.lease_generation or 0)
            previous_owner = run.lease_owner
            previous_expiry = _as_utc(run.lease_expires_at)
            run.lease_generation = previous_generation + 1
            run.lease_owner = self.worker_id
            run.heartbeat_at = now
            run.lease_expires_at = self._deadline(now)
            run.updated_at = now
            write_audit(
                db,
                actor=self.audit_actor,
                action=(
                    "execution.lease_recovered"
                    if previous_generation > 0
                    else "execution.lease_claimed"
                ),
                entity_type="execution",
                entity_id=run.id,
                details={
                    "generation": run.lease_generation,
                    "status": run.status,
                    "previous_owner": previous_owner,
                    "previous_lease_expires_at": (
                        previous_expiry.isoformat() if previous_expiry else None
                    ),
                },
            )
            leases.append(
                ExecutionLease(
                    execution_id=run.id,
                    generation=run.lease_generation,
                    status=run.status,
                    opencode_session_id=run.opencode_session_id,
                )
            )
        db.commit()
        return leases

    def _locked_owned_run(
        self,
        db: Session,
        lease: ExecutionLease,
        now: datetime,
    ) -> ExecutionRun | None:
        run = db.scalar(
            select(ExecutionRun)
            .where(ExecutionRun.id == lease.execution_id)
            .with_for_update()
        )
        if run is None or run.status not in ACTIVE_EXECUTION_STATUSES:
            return None
        expires_at = _as_utc(run.lease_expires_at)
        if (
            run.lease_owner != self.worker_id
            or int(run.lease_generation or 0) != lease.generation
            or expires_at is None
            or expires_at <= now
        ):
            return None
        return run

    def heartbeat(
        self,
        db: Session,
        lease: ExecutionLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None:
            db.rollback()
            return False
        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        db.commit()
        return True

    def persist_dispatch_session(
        self,
        db: Session,
        lease: ExecutionLease,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.status != "queued":
            db.rollback()
            return False
        if run.opencode_session_id and run.opencode_session_id != session_id:
            db.rollback()
            return False
        run.opencode_session_id = session_id
        run.stage = "dispatch_session_ready"
        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        db.commit()
        return True

    def mark_dispatched(
        self,
        db: Session,
        lease: ExecutionLease,
        session_id: str,
        message_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.status != "queued":
            db.rollback()
            return False
        if run.opencode_session_id != session_id:
            db.rollback()
            return False
        run.status = "running"
        run.stage = "department_lead"
        run.error = ""
        run.heartbeat_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="execution.dispatched",
            entity_type="execution",
            entity_id=run.id,
            details={
                "task_id": run.task_id,
                "generation": lease.generation,
                "session_id": session_id,
                "message_id": message_id,
            },
        )
        db.commit()
        return True

    def apply_observation(
        self,
        db: Session,
        lease: ExecutionLease,
        *,
        state_type: str,
        result: str,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.status != "running":
            db.rollback()
            return "lost"

        if state_type == "idle" and result.strip():
            run.status = "completed"
            run.stage = "manager_review"
            run.result = result.strip()
            run.error = ""
            run.finished_at = now
            run.heartbeat_at = now
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = now
            task = db.get(Task, run.task_id)
            if task and task.status in {"in_progress", "waiting_approval"}:
                task.status = "qa"
                task.updated_at = now
            write_audit(
                db,
                actor=self.audit_actor,
                action="execution.completed",
                entity_type="execution",
                entity_id=run.id,
                details={
                    "task_id": run.task_id,
                    "generation": lease.generation,
                },
            )
            db.commit()
            return "completed"

        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        db.commit()
        return "running"


def _queued_dispatch_context(
    manager: ExecutionLeaseManager,
    lease: ExecutionLease,
) -> tuple[str | None, str, str] | None:
    """Read a fenced queued run without holding its DB transaction over network I/O."""
    with SessionLocal() as db:
        now = utc_now()
        run = manager._locked_owned_run(db, lease, now)
        if run is None or run.status != "queued":
            db.rollback()
            return None
        task = db.get(Task, run.task_id)
        if task is None:
            db.rollback()
            return None
        session_id = run.opencode_session_id
        title = execution_session_title(task, run.id)
        prompt = execution_prompt(task)
        db.rollback()
        return session_id, title, prompt


def dispatch_execution(
    manager: ExecutionLeaseManager,
    client: OpenCodeClient,
    lease: ExecutionLease,
) -> str:
    context = _queued_dispatch_context(manager, lease)
    if context is None:
        return "lost"
    session_id, title, prompt = context

    if not session_id:
        matches = client.sessions_for_execution(lease.execution_id)
        if len(matches) > 1:
            raise OpenCodeError(
                f"Ambiguous OpenCode dispatch recovery for execution {lease.execution_id}: "
                f"{len(matches)} sessions carry the same metadata"
            )
        if matches:
            session = matches[0]
        else:
            session = client.create_session(
                title,
                metadata={EXECUTION_METADATA_KEY: lease.execution_id},
            )
        session_id = str(session.get("id") or session.get("sessionID") or "")
        if not session_id:
            raise OpenCodeError("OpenCode не вернул session id")
        with SessionLocal() as db:
            if not manager.persist_dispatch_session(db, lease, session_id):
                return "lost"

    message_id = execution_message_id(lease.execution_id)
    existing_message = client.message(session_id, message_id)
    if existing_message is None:
        client.prompt_async(session_id, prompt, message_id=message_id)

    with SessionLocal() as db:
        if not manager.mark_dispatched(db, lease, session_id, message_id):
            return "lost"
    return "dispatched"


def poll_execution(
    manager: ExecutionLeaseManager,
    client_factory: Callable[[], OpenCodeClient],
    lease: ExecutionLease,
) -> str:
    try:
        client = client_factory()
        if lease.status == "queued":
            return dispatch_execution(manager, client, lease)
        if not lease.opencode_session_id:
            raise OpenCodeError(
                f"Running execution {lease.execution_id} has no OpenCode session id"
            )
        statuses = client.session_statuses()
        messages = client.messages(lease.opencode_session_id)
        state = statuses.get(lease.opencode_session_id) or {}
        state_type = state.get("type") if isinstance(state, dict) else str(state)
        result = extract_last_assistant_text(messages)
    except OpenCodeError as exc:
        LOGGER.warning(
            "OpenCode operation failed execution=%s generation=%s status=%s: %s",
            lease.execution_id,
            lease.generation,
            lease.status,
            exc,
        )
        with SessionLocal() as db:
            return "running" if manager.heartbeat(db, lease) else "lost"

    with SessionLocal() as db:
        outcome = manager.apply_observation(
            db,
            lease,
            state_type=state_type or "unknown",
            result=result,
        )
    if outcome == "lost":
        LOGGER.warning(
            "Rejected stale execution observation execution=%s generation=%s",
            lease.execution_id,
            lease.generation,
        )
    return outcome


def run_forever(
    manager: ExecutionLeaseManager,
    client_factory: Callable[[], OpenCodeClient],
    *,
    poll_seconds: float,
    max_active: int,
) -> None:
    active: dict[str, ExecutionLease] = {}
    with ThreadPoolExecutor(
        max_workers=max_active,
        thread_name_prefix="execution-poll",
    ) as pool:
        while True:
            slots = max_active - len(active)
            if slots > 0:
                with SessionLocal() as db:
                    claimed = manager.claim_available(db, limit=slots)
                for lease in claimed:
                    active[lease.execution_id] = lease

            futures = {
                pool.submit(poll_execution, manager, client_factory, lease): lease
                for lease in list(active.values())
            }
            for future in as_completed(futures):
                lease = futures[future]
                try:
                    outcome = future.result()
                except Exception:
                    # Worker boundary: never let an unexpected process-level error
                    # commit stale state; the lease expires and another generation recovers.
                    LOGGER.exception(
                        "Unexpected worker failure execution=%s generation=%s",
                        lease.execution_id,
                        lease.generation,
                    )
                    active.pop(lease.execution_id, None)
                    continue
                if outcome != "running":
                    active.pop(lease.execution_id, None)

            time.sleep(poll_seconds)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    worker_id = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid4().hex[:12]}"
    lease_seconds = _positive_int(
        "CONTROL_PLANE_EXECUTION_WORKER_LEASE_SECONDS",
        120,
        minimum=30,
        maximum=3600,
    )
    max_active = _positive_int(
        "CONTROL_PLANE_EXECUTION_WORKER_MAX_ACTIVE",
        4,
        minimum=1,
        maximum=32,
    )
    poll_seconds = _positive_float(
        "CONTROL_PLANE_EXECUTION_WORKER_POLL_SECONDS",
        5.0,
        minimum=0.5,
    )

    with SessionLocal() as db:
        assert_database_shape(db.get_bind())

    manager = ExecutionLeaseManager(worker_id, lease_seconds=lease_seconds)

    def client_factory() -> OpenCodeClient:
        return OpenCodeClient(
            settings.opencode_internal_url,
            settings.opencode_username,
            settings.opencode_password,
        )

    LOGGER.info(
        "Execution worker started worker_id=%s lease_seconds=%s max_active=%s poll_seconds=%s",
        worker_id,
        lease_seconds,
        max_active,
        poll_seconds,
    )
    run_forever(
        manager,
        client_factory,
        poll_seconds=poll_seconds,
        max_active=max_active,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
