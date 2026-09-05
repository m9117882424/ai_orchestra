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
from .models import ExecutionRun, Task
from .opencode_client import OpenCodeClient, OpenCodeError, extract_last_assistant_text
from .schema import assert_database_shape
from .services import write_audit
from .settings import get_settings


LOGGER = logging.getLogger("ai_orchestra.execution_worker")


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
    opencode_session_id: str


class ExecutionLeaseManager:
    """PostgreSQL-backed lease and fencing boundary for execution completion.

    A result may change durable state only while the caller still owns an unexpired
    lease with the exact generation it claimed. Expired/zombie workers therefore
    cannot commit a late success after another worker recovered the execution.
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
                    ExecutionRun.status == "running",
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
        if run is None or run.status != "running":
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
        if run is None:
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


def poll_execution(
    manager: ExecutionLeaseManager,
    client_factory: Callable[[], OpenCodeClient],
    lease: ExecutionLease,
) -> str:
    try:
        client = client_factory()
        statuses = client.session_statuses()
        messages = client.messages(lease.opencode_session_id)
        state = statuses.get(lease.opencode_session_id) or {}
        state_type = state.get("type") if isinstance(state, dict) else str(state)
        result = extract_last_assistant_text(messages)
    except OpenCodeError as exc:
        LOGGER.warning(
            "OpenCode poll failed execution=%s generation=%s: %s",
            lease.execution_id,
            lease.generation,
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
                except Exception:  # worker boundary: let lease expire and recover safely.
                    LOGGER.exception(
                        "Unexpected poll failure execution=%s generation=%s",
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
