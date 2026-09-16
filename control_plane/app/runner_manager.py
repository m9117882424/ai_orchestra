from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import ExecutionRun, Repository, RunnerJob, TaskWorkspace
from .services import write_audit

LOGGER = logging.getLogger("ai_orchestra.runner_manager")
RUNNABLE_EXECUTION_STATUSES = ("running", "completed")
RUNNABLE_WORKSPACE_STATUSES = ("ready", "inspection_pending", "inspecting", "retained")
TERMINAL_RUNNER_STATUSES = ("completed", "failed", "timed_out", "cleanup_uncertain")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


class RunnerUnavailable(RuntimeError):
    """The broker was not reached, so no runner side effect was submitted."""


class RunnerOutcomeUnknown(RuntimeError):
    """A request may have reached runnerd, so immediate retry is forbidden."""


@dataclass(frozen=True)
class RunnerJobLease:
    job_id: str
    execution_id: str
    repository_id: str
    workspace_id: str
    generation: int
    argv: tuple[str, ...]
    timeout_seconds: int
    base_commit: str
    preflight_digest: str
    source_snapshot_digest: str | None


class RunnerdClient:
    def __init__(self, socket_path: Path, *, response_padding_seconds: int = 150):
        if not socket_path.is_absolute():
            raise ValueError("runnerd socket path must be absolute")
        if not 60 <= response_padding_seconds <= 600:
            raise ValueError("response padding must be between 60 and 600 seconds")
        self.socket_path = socket_path
        self.response_padding_seconds = response_padding_seconds

    @staticmethod
    def _payload(lease: RunnerJobLease) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1,
            "operation": "run",
            "request_id": lease.job_id,
            "workspace_id": lease.workspace_id,
            "execution_id": lease.execution_id,
            "base_commit": lease.base_commit,
            "preflight_digest": lease.preflight_digest,
            "argv": list(lease.argv),
            "timeout_seconds": lease.timeout_seconds,
        }
        if lease.source_snapshot_digest is not None:
            payload["source_snapshot_digest"] = lease.source_snapshot_digest
        return payload

    def run(self, lease: RunnerJobLease) -> dict[str, Any]:
        payload = self._payload(lease)
        encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        submitted = False
        try:
            client.settimeout(10)
            try:
                client.connect(str(self.socket_path))
            except OSError as exc:
                raise RunnerUnavailable(type(exc).__name__) from exc
            try:
                client.sendall(encoded)
                submitted = True
            except OSError as exc:
                raise RunnerOutcomeUnknown(type(exc).__name__) from exc

            client.settimeout(lease.timeout_seconds + self.response_padding_seconds)
            chunks = bytearray()
            while not chunks.endswith(b"\n"):
                try:
                    part = client.recv(65536)
                except OSError as exc:
                    raise RunnerOutcomeUnknown(type(exc).__name__) from exc
                if not part:
                    raise RunnerOutcomeUnknown("runnerd_closed_without_response")
                chunks.extend(part)
                if len(chunks) > 4_194_304:
                    raise RunnerOutcomeUnknown("runnerd_response_too_large")
            try:
                response = json.loads(chunks)
            except json.JSONDecodeError as exc:
                raise RunnerOutcomeUnknown("runnerd_invalid_json") from exc
            if not isinstance(response, dict):
                raise RunnerOutcomeUnknown("runnerd_invalid_response")
            return response
        finally:
            try:
                client.close()
            except OSError:
                pass
            if submitted:
                LOGGER.debug("runnerd request submitted job=%s", lease.job_id)


class RunnerJobLeaseManager:
    def __init__(
        self,
        worker_id: str,
        *,
        lease_padding_seconds: int = 180,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
    ):
        if not worker_id or len(worker_id) > 160:
            raise ValueError("invalid worker id")
        if not 120 <= lease_padding_seconds <= 900:
            raise ValueError("lease padding must be between 120 and 900 seconds")
        if not 1 <= retry_base_seconds <= 3600:
            raise ValueError("retry base must be between 1 and 3600 seconds")
        if not retry_base_seconds <= retry_max_seconds <= 86400:
            raise ValueError("retry max must be >= retry base and <= 86400 seconds")
        self.worker_id = worker_id
        self.lease_padding_seconds = lease_padding_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    @property
    def audit_actor(self) -> str:
        return f"runner-manager:{self.worker_id}"[:100]

    def _deadline(self, now: datetime, timeout_seconds: int) -> datetime:
        return now + timedelta(seconds=timeout_seconds + self.lease_padding_seconds)

    def _retry_delay(self, failure_count: int) -> int:
        exponent = min(max(failure_count, 0), 16)
        return min(self.retry_base_seconds * (2**exponent), self.retry_max_seconds)

    def claim_available(
        self,
        db: Session,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> list[RunnerJobLease]:
        if not 1 <= limit <= 16:
            raise ValueError("limit must be between 1 and 16")
        now = now or utc_now()
        jobs = list(
            db.scalars(
                select(RunnerJob)
                .where(
                    or_(
                        and_(
                            RunnerJob.status == "queued",
                            RunnerJob.next_attempt_at.is_not(None),
                            RunnerJob.next_attempt_at <= now,
                        ),
                        and_(
                            RunnerJob.status == "running",
                            RunnerJob.lease_expires_at.is_not(None),
                            RunnerJob.lease_expires_at <= now,
                        ),
                    )
                )
                .order_by(RunnerJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        leases: list[RunnerJobLease] = []
        for job in jobs:
            recovered = job.status == "running"
            previous_owner = job.lease_owner
            previous_generation = int(job.lease_generation or 0)
            job.status = "running"
            job.next_attempt_at = None
            job.lease_generation = previous_generation + 1
            job.lease_owner = self.worker_id
            job.heartbeat_at = now
            job.lease_expires_at = self._deadline(now, job.timeout_seconds)
            if job.started_at is None:
                job.started_at = now
            job.updated_at = now
            write_audit(
                db,
                actor=self.audit_actor,
                action=("runner_job.lease_recovered" if recovered else "runner_job.lease_claimed"),
                entity_type="runner_job",
                entity_id=job.id,
                details={
                    "execution_id": job.execution_id,
                    "workspace_id": job.workspace_id,
                    "generation": job.lease_generation,
                    "previous_generation": previous_generation,
                    "previous_owner": previous_owner,
                },
            )
            leases.append(
                RunnerJobLease(
                    job_id=job.id,
                    execution_id=job.execution_id,
                    repository_id=job.repository_id,
                    workspace_id=job.workspace_id,
                    generation=job.lease_generation,
                    argv=tuple(str(item) for item in job.argv),
                    timeout_seconds=job.timeout_seconds,
                    base_commit=job.base_commit,
                    preflight_digest=job.preflight_digest,
                    source_snapshot_digest=job.source_snapshot_digest,
                )
            )
        db.commit()
        return leases

    def _locked_owned_job(
        self,
        db: Session,
        lease: RunnerJobLease,
        now: datetime,
    ) -> RunnerJob | None:
        job = db.scalar(
            select(RunnerJob)
            .where(RunnerJob.id == lease.job_id)
            .with_for_update()
        )
        if job is None:
            return None
        expires_at = _as_utc(job.lease_expires_at)
        if (
            job.status != "running"
            or job.lease_owner != self.worker_id
            or int(job.lease_generation or 0) != lease.generation
            or expires_at is None
            or expires_at <= now
        ):
            return None
        return job

    def _finish_rejected(
        self,
        db: Session,
        job: RunnerJob,
        code: str,
        now: datetime,
    ) -> None:
        job.status = "rejected"
        job.exit_code = None
        job.stdout = ""
        job.stderr = ""
        job.output_truncated = False
        job.cleanup_confirmed = True
        job.runner_image_id = None
        job.last_error_code = code
        job.finished_at = now
        job.heartbeat_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="runner_job.rejected",
            entity_type="runner_job",
            entity_id=job.id,
            details={"execution_id": job.execution_id, "code": code},
        )

    def authorize_binding(
        self,
        db: Session,
        lease: RunnerJobLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        job = self._locked_owned_job(db, lease, now)
        if job is None:
            db.rollback()
            return False
        code: str | None = None
        run = db.scalar(
            select(ExecutionRun)
            .where(ExecutionRun.id == job.execution_id)
            .with_for_update()
        )
        repository = db.scalar(
            select(Repository)
            .where(Repository.id == job.repository_id)
            .with_for_update()
        )
        workspace = db.scalar(
            select(TaskWorkspace)
            .where(TaskWorkspace.id == job.workspace_id)
            .with_for_update()
        )
        if run is None or repository is None or workspace is None:
            code = "runner_binding_missing"
        elif (
            run.contract_version != 2
            or run.repository_id != job.repository_id
            or run.workspace_id != job.workspace_id
            or run.base_commit != job.base_commit
            or run.workspace_preflight_digest != job.preflight_digest
        ):
            code = "runner_binding_changed"
        elif (
            run.status not in RUNNABLE_EXECUTION_STATUSES
            or run.cancel_requested_at is not None
        ):
            code = "execution_not_runnable"
        elif (
            run.workspace_runtime_verified_at is None
            or run.workspace_runtime_preflight_digest != job.preflight_digest
        ):
            code = "runtime_preflight_missing"
        elif not repository.enabled or repository.status != "ready":
            code = "repository_trust_revoked"
        elif (
            workspace.repository_id != job.repository_id
            or workspace.base_commit != job.base_commit
            or workspace.preflight_digest != job.preflight_digest
        ):
            code = "workspace_binding_changed"
        elif workspace.status not in RUNNABLE_WORKSPACE_STATUSES:
            code = "workspace_not_runnable"

        if code is not None:
            self._finish_rejected(db, job, code, now)
            db.commit()
            return False

        job.heartbeat_at = now
        job.lease_expires_at = self._deadline(now, job.timeout_seconds)
        job.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="runner_job.authorized",
            entity_type="runner_job",
            entity_id=job.id,
            details={
                "execution_id": job.execution_id,
                "repository_id": job.repository_id,
                "workspace_id": job.workspace_id,
                "generation": lease.generation,
                "base_commit": job.base_commit,
                "preflight_digest": job.preflight_digest,
                "source_snapshot_digest": job.source_snapshot_digest,
                "checkpoint_digest": job.checkpoint_digest,
                "checkpoint_command_index": job.checkpoint_command_index,
            },
        )
        db.commit()
        return True

    def mark_retry(
        self,
        db: Session,
        lease: RunnerJobLease,
        code: str,
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        job = self._locked_owned_job(db, lease, now)
        if job is None:
            db.rollback()
            return "lost"
        delay = self._retry_delay(job.failure_count)
        job.failure_count += 1
        job.status = "queued"
        job.next_attempt_at = now + timedelta(seconds=delay)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = now
        job.last_error_code = code
        job.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="runner_job.retry_scheduled",
            entity_type="runner_job",
            entity_id=job.id,
            details={
                "execution_id": job.execution_id,
                "generation": lease.generation,
                "code": code,
                "delay_seconds": delay,
                "failure_count": job.failure_count,
            },
        )
        db.commit()
        return "queued"

    def mark_rejected_response(
        self,
        db: Session,
        lease: RunnerJobLease,
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        job = self._locked_owned_job(db, lease, now)
        if job is None:
            db.rollback()
            return "lost"
        self._finish_rejected(db, job, "runner_rejected", now)
        db.commit()
        return "rejected"

    def mark_uncertain(
        self,
        db: Session,
        lease: RunnerJobLease,
        code: str,
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        job = self._locked_owned_job(db, lease, now)
        if job is None:
            db.rollback()
            return "lost"
        job.status = "cleanup_uncertain"
        job.cleanup_confirmed = False
        job.last_error_code = code
        job.finished_at = now
        job.heartbeat_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="runner_job.cleanup_uncertain",
            entity_type="runner_job",
            entity_id=job.id,
            details={
                "execution_id": job.execution_id,
                "generation": lease.generation,
                "code": code,
            },
        )
        db.commit()
        return "cleanup_uncertain"

    def mark_terminal(
        self,
        db: Session,
        lease: RunnerJobLease,
        response: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        job = self._locked_owned_job(db, lease, now)
        if job is None:
            db.rollback()
            return "lost"
        status = str(response.get("status") or "")
        if status not in TERMINAL_RUNNER_STATUSES:
            db.rollback()
            return "invalid"
        if (
            response.get("request_id") != job.id
            or response.get("execution_id") != job.execution_id
            or response.get("workspace_id") != job.workspace_id
        ):
            db.rollback()
            return self.mark_uncertain(db, lease, "runner_identity_mismatch", now=now)

        image_id = response.get("runner_image_id")
        cleanup_confirmed = response.get("cleanup_confirmed")
        exit_code = response.get("exit_code")
        if status in {"completed", "failed", "timed_out"}:
            if (
                not isinstance(image_id, str)
                or len(image_id) != 71
                or not image_id.startswith("sha256:")
                or cleanup_confirmed is not True
            ):
                db.rollback()
                return self.mark_uncertain(db, lease, "runner_terminal_evidence_invalid", now=now)
        if status in {"completed", "failed"} and not isinstance(exit_code, int):
            db.rollback()
            return self.mark_uncertain(db, lease, "runner_exit_code_invalid", now=now)
        if status == "timed_out" and exit_code is not None:
            db.rollback()
            return self.mark_uncertain(db, lease, "runner_timeout_evidence_invalid", now=now)
        if status == "cleanup_uncertain" and cleanup_confirmed is not False:
            db.rollback()
            return self.mark_uncertain(db, lease, "runner_cleanup_evidence_invalid", now=now)

        job.status = status
        job.runner_image_id = image_id if isinstance(image_id, str) else None
        job.exit_code = exit_code if isinstance(exit_code, int) else None
        job.stdout = str(response.get("stdout") or "")
        job.stderr = str(response.get("stderr") or "")
        job.output_truncated = bool(response.get("output_truncated"))
        job.cleanup_confirmed = bool(cleanup_confirmed)
        job.last_error_code = None if status == "completed" else f"runner_{status}"
        job.finished_at = now
        job.heartbeat_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action=f"runner_job.{status}",
            entity_type="runner_job",
            entity_id=job.id,
            details={
                "execution_id": job.execution_id,
                "generation": lease.generation,
                "exit_code": job.exit_code,
                "output_truncated": job.output_truncated,
                "cleanup_confirmed": job.cleanup_confirmed,
                "runner_image_id": job.runner_image_id,
            },
        )
        db.commit()
        return status


def process_runner_job(
    manager: RunnerJobLeaseManager,
    client: RunnerdClient,
    lease: RunnerJobLease,
) -> str:
    with SessionLocal() as db:
        if not manager.authorize_binding(db, lease):
            return "rejected_or_lost"
    try:
        response = client.run(lease)
    except RunnerUnavailable:
        with SessionLocal() as db:
            return manager.mark_retry(db, lease, "runnerd_unavailable")
    except RunnerOutcomeUnknown as exc:
        LOGGER.warning(
            "Runner outcome unknown job=%s generation=%s reason=%s; waiting for lease expiry",
            lease.job_id,
            lease.generation,
            exc,
        )
        return "outcome_unknown"

    status = str(response.get("status") or "")
    if status == "busy":
        if response.get("request_id") not in {None, lease.job_id}:
            with SessionLocal() as db:
                return manager.mark_uncertain(db, lease, "runner_busy_identity_mismatch")
        with SessionLocal() as db:
            return manager.mark_retry(db, lease, "runnerd_busy")
    if status == "rejected":
        with SessionLocal() as db:
            return manager.mark_rejected_response(db, lease)
    if status == "error":
        with SessionLocal() as db:
            return manager.mark_uncertain(db, lease, "runnerd_internal_error")
    if status not in TERMINAL_RUNNER_STATUSES:
        with SessionLocal() as db:
            return manager.mark_uncertain(db, lease, "runner_protocol_status_invalid")
    with SessionLocal() as db:
        return manager.mark_terminal(db, lease, response)


def write_worker_health(path: Path) -> None:
    path.touch(exist_ok=True)


def run_forever(
    manager: RunnerJobLeaseManager,
    client: RunnerdClient,
    *,
    poll_seconds: int,
    max_active: int,
    health_path: Path,
) -> None:
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    futures = {}
    with ThreadPoolExecutor(max_workers=max_active, thread_name_prefix="runner-job") as pool:
        while True:
            write_worker_health(health_path)
            done = set()
            if futures:
                done, _ = wait(
                    set(futures),
                    timeout=poll_seconds,
                    return_when=FIRST_COMPLETED,
                )
            for future in done:
                lease = futures.pop(future)
                try:
                    outcome = future.result()
                except Exception:
                    LOGGER.exception(
                        "Uncontained Runner Manager failure job=%s generation=%s",
                        lease.job_id,
                        lease.generation,
                    )
                    outcome = "outcome_unknown"
                LOGGER.info(
                    "runner job processed job=%s generation=%s outcome=%s",
                    lease.job_id,
                    lease.generation,
                    outcome,
                )

            slots = max_active - len(futures)
            if slots > 0:
                with SessionLocal() as db:
                    claimed = manager.claim_available(db, limit=slots)
                for lease in claimed:
                    future = pool.submit(process_runner_job, manager, client, lease)
                    futures[future] = lease
            write_worker_health(health_path)
            if not futures:
                time.sleep(poll_seconds)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker_id = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid4().hex[:12]}"
    lease_padding = _bounded_int(
        "RUNNER_MANAGER_LEASE_PADDING_SECONDS", 180, minimum=120, maximum=900
    )
    response_padding = _bounded_int(
        "RUNNER_MANAGER_RESPONSE_PADDING_SECONDS", 150, minimum=60, maximum=600
    )
    if lease_padding < response_padding + 30:
        raise RuntimeError(
            "RUNNER_MANAGER_LEASE_PADDING_SECONDS must exceed response padding by 30s"
        )
    retry_base = _bounded_int(
        "RUNNER_MANAGER_RETRY_BASE_SECONDS", 5, minimum=1, maximum=3600
    )
    retry_max = _bounded_int(
        "RUNNER_MANAGER_RETRY_MAX_SECONDS", 300, minimum=1, maximum=86400
    )
    if retry_max < retry_base:
        raise RuntimeError("RUNNER_MANAGER_RETRY_MAX_SECONDS must be >= retry base")
    max_active = _bounded_int("RUNNER_MANAGER_MAX_ACTIVE", 2, minimum=1, maximum=8)
    poll_seconds = _bounded_int("RUNNER_MANAGER_POLL_SECONDS", 2, minimum=1, maximum=60)
    socket_path = Path(os.getenv("RUNNER_MANAGER_SOCKET_PATH", "/run/ai-orchestra/runnerd.sock"))
    health_path = Path(
        os.getenv(
            "RUNNER_MANAGER_HEALTH_PATH",
            "/tmp/ai-orchestra-runner-manager.heartbeat",
        )
    )
    manager = RunnerJobLeaseManager(
        worker_id,
        lease_padding_seconds=lease_padding,
        retry_base_seconds=retry_base,
        retry_max_seconds=retry_max,
    )
    client = RunnerdClient(socket_path, response_padding_seconds=response_padding)
    run_forever(
        manager,
        client,
        poll_seconds=poll_seconds,
        max_active=max_active,
        health_path=health_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
