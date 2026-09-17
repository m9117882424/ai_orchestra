from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest

from control_plane.app.db import SessionLocal
from control_plane.app.models import ExecutionRun, Repository, RunnerJob, Task, TaskWorkspace
from control_plane.app.runner_manager import (
    RunnerJobLeaseManager,
    RunnerOutcomeUnknown,
    RunnerUnavailable,
    RunnerdClient,
    process_runner_job,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_job(*, timeout_seconds: int = 30) -> str:
    now = _now()
    repository_id = str(uuid4())
    task_id = str(uuid4())
    workspace_id = str(uuid4())
    execution_id = str(uuid4())
    job_id = str(uuid4())
    commit = "a" * 40
    tree = "b" * 40
    digest = "c" * 64
    with SessionLocal() as db:
        db.add(
            Repository(
                id=repository_id,
                name=f"runner-{repository_id}",
                remote_url=f"https://github.com/example/{repository_id}.git",
                remote_identity=f"github.com/example/{repository_id}",
                remote_host="github.com",
                provider="github",
                enabled=True,
                status="ready",
                default_branch="main",
                last_known_commit=commit,
                last_fetched_at=now,
                sync_finished_at=now,
                sync_next_at=now,
            )
        )
        db.add(
            Task(
                id=task_id,
                title="Durable runner job",
                repository_id=repository_id,
                status="in_progress",
            )
        )
        db.add(
            TaskWorkspace(
                id=workspace_id,
                task_id=task_id,
                repository_id=repository_id,
                status="ready",
                base_commit=commit,
                base_branch="main",
                branch_name=f"ai-orchestra/task-{task_id[:12]}/run-{execution_id}",
                opencode_path=f"/workspace/worktrees/managed/{workspace_id}",
                initial_tree=tree,
                preflight_digest=digest,
                tracked_entries=2,
                prepared_at=now,
                version=1,
            )
        )
        db.add(
            ExecutionRun(
                id=execution_id,
                task_id=task_id,
                contract_version=2,
                repository_id=repository_id,
                workspace_id=workspace_id,
                base_commit=commit,
                workspace_path=f"/workspace/worktrees/managed/{workspace_id}",
                workspace_tree=tree,
                workspace_preflight_digest=digest,
                workspace_preflight_completed_at=now,
                workspace_runtime_preflight_digest=digest,
                workspace_runtime_verified_at=now,
                status="running",
                stage="department_lead",
                opencode_session_id=f"session-{execution_id}",
            )
        )
        db.flush()
        db.add(
            RunnerJob(
                id=job_id,
                execution_id=execution_id,
                repository_id=repository_id,
                workspace_id=workspace_id,
                idempotency_key=str(uuid4()),
                status="queued",
                argv=["python3", "-c", "print('runner-ok')"],
                timeout_seconds=timeout_seconds,
                base_commit=commit,
                preflight_digest=digest,
                stdout="",
                stderr="",
                output_truncated=False,
                cleanup_confirmed=None,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()
    return job_id


def _claim(manager: RunnerJobLeaseManager, job_id: str, now: datetime | None = None):
    with SessionLocal() as db:
        leases = manager.claim_available(db, limit=1, now=now)
    assert len(leases) == 1
    assert leases[0].job_id == job_id
    return leases[0]


def _terminal_response(lease, *, status="completed", exit_code=0):
    return {
        "version": 1,
        "request_id": lease.job_id,
        "workspace_id": lease.workspace_id,
        "execution_id": lease.execution_id,
        "status": status,
        "exit_code": exit_code,
        "stdout": "runner-ok\n",
        "stderr": "",
        "output_truncated": False,
        "runner_image_id": "sha256:" + "d" * 64,
        "cleanup_confirmed": True,
    }


def test_claim_and_stale_generation_are_fenced():
    job_id = _seed_job(timeout_seconds=10)
    first_manager = RunnerJobLeaseManager("worker-a", lease_padding_seconds=120)
    second_manager = RunnerJobLeaseManager("worker-b", lease_padding_seconds=120)
    start = _now()
    first = _claim(first_manager, job_id, start)
    assert first.generation == 1

    recovery_time = start + timedelta(seconds=131)
    second = _claim(second_manager, job_id, recovery_time)
    assert second.generation == 2

    with SessionLocal() as db:
        stale = first_manager.mark_terminal(
            db,
            first,
            _terminal_response(first),
            now=recovery_time + timedelta(seconds=1),
        )
    assert stale == "lost"
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        assert job.status == "running"
        assert job.lease_owner == "worker-b"
        assert job.lease_generation == 2


def test_authorization_rechecks_repository_trust_before_runner_side_effect():
    job_id = _seed_job()
    manager = RunnerJobLeaseManager("worker-a")
    lease = _claim(manager, job_id)
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        repository = db.get(Repository, job.repository_id)
        repository.enabled = False
        repository.status = "unavailable"
        repository.last_known_commit = None
        repository.last_fetched_at = None
        repository.sync_finished_at = None
        db.commit()
    with SessionLocal() as db:
        authorized = manager.authorize_binding(db, lease)
    assert authorized is False
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        assert job.status == "rejected"
        assert job.last_error_code == "repository_trust_revoked"
        assert job.cleanup_confirmed is True
        assert job.lease_owner is None


def test_runner_unavailable_is_safe_retry_after_authorization():
    job_id = _seed_job()
    manager = RunnerJobLeaseManager(
        "worker-a", retry_base_seconds=7, retry_max_seconds=60
    )
    lease = _claim(manager, job_id)

    class UnavailableClient:
        def run(self, _lease):
            raise RunnerUnavailable("connect_failed")

    outcome = process_runner_job(manager, UnavailableClient(), lease)
    assert outcome == "queued"
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        assert job.status == "queued"
        assert job.failure_count == 1
        assert job.last_error_code == "runnerd_unavailable"
        assert job.next_attempt_at is not None
        assert job.lease_owner is None


def test_unknown_runner_outcome_keeps_fenced_running_lease_until_expiry():
    job_id = _seed_job()
    manager = RunnerJobLeaseManager("worker-a")
    lease = _claim(manager, job_id)

    class UnknownClient:
        def run(self, _lease):
            raise RunnerOutcomeUnknown("response_lost")

    outcome = process_runner_job(manager, UnknownClient(), lease)
    assert outcome == "outcome_unknown"
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        assert job.status == "running"
        assert job.lease_owner == "worker-a"
        assert job.lease_generation == lease.generation
        assert job.failure_count == 0
        assert job.finished_at is None


def test_valid_terminal_response_commits_evidence_and_releases_lease():
    job_id = _seed_job()
    manager = RunnerJobLeaseManager("worker-a")
    lease = _claim(manager, job_id)
    with SessionLocal() as db:
        assert manager.authorize_binding(db, lease) is True
    with SessionLocal() as db:
        outcome = manager.mark_terminal(db, lease, _terminal_response(lease))
    assert outcome == "completed"
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        assert job.status == "completed"
        assert job.exit_code == 0
        assert job.stdout == "runner-ok\n"
        assert job.cleanup_confirmed is True
        assert job.runner_image_id == "sha256:" + "d" * 64
        assert job.lease_owner is None
        assert job.finished_at is not None


def test_identity_mismatch_fails_closed_as_cleanup_uncertain():
    job_id = _seed_job()
    manager = RunnerJobLeaseManager("worker-a")
    lease = _claim(manager, job_id)
    with SessionLocal() as db:
        assert manager.authorize_binding(db, lease) is True
    response = _terminal_response(lease)
    response["workspace_id"] = str(uuid4())
    with SessionLocal() as db:
        outcome = manager.mark_terminal(db, lease, response)
    assert outcome == "cleanup_uncertain"
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        assert job.status == "cleanup_uncertain"
        assert job.cleanup_confirmed is False
        assert job.last_error_code == "runner_identity_mismatch"
        assert job.lease_owner is None


def test_runnerd_payload_exposes_only_declared_protocol_fields():
    job_id = _seed_job()
    manager = RunnerJobLeaseManager("worker-a")
    lease = _claim(manager, job_id)
    payload = RunnerdClient._payload(lease)
    assert set(payload) == {
        "version",
        "operation",
        "request_id",
        "workspace_id",
        "execution_id",
        "base_commit",
        "preflight_digest",
        "argv",
        "timeout_seconds",
    }
    forbidden = {"volume", "host_path", "network", "environment", "secrets", "image"}
    assert forbidden.isdisjoint(payload)


def test_runnerd_payload_carries_optional_source_snapshot_digest():
    job_id = _seed_job()
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        job.source_snapshot_digest = "e" * 64
        db.commit()
    manager = RunnerJobLeaseManager("worker-a")
    lease = _claim(manager, job_id)
    payload = RunnerdClient._payload(lease)
    assert payload["source_snapshot_digest"] == "e" * 64


def test_runnerd_payload_omits_snapshot_for_legacy_manual_job():
    job_id = _seed_job()
    manager = RunnerJobLeaseManager("worker-a")
    lease = _claim(manager, job_id)
    payload = RunnerdClient._payload(lease)
    assert "source_snapshot_digest" not in payload


@pytest.mark.parametrize("updates", [
    {"exit_code": 1},
    {"exit_code": False},
    {"status": "failed", "exit_code": 0},
    {"runner_image_id": "sha256:" + "z" * 64},
    {"version": 2},
    {"output_truncated": "false"},
    {"stdout": {"unexpected": "object"}},
])
def test_inconsistent_terminal_evidence_fails_closed(updates):
    job_id = _seed_job()
    manager = RunnerJobLeaseManager("worker-a")
    lease = _claim(manager, job_id)
    response = _terminal_response(lease)
    response.update(updates)
    with SessionLocal() as db:
        outcome = manager.mark_terminal(db, lease, response)
    assert outcome == "cleanup_uncertain"
    with SessionLocal() as db:
        job = db.get(RunnerJob, job_id)
        assert job.status == "cleanup_uncertain"
        assert job.cleanup_confirmed is False
