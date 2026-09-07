from datetime import datetime, timedelta, timezone

import pytest

from control_plane.app.db import SessionLocal
from control_plane.app.execution_protocol import (
    EXECUTION_METADATA_KEY,
    execution_message_id,
    execution_part_id,
)
from control_plane.app.execution_worker import (
    ExecutionLeaseManager,
    cancel_execution,
    dispatch_execution,
    poll_execution,
    timeout_execution,
    write_worker_health,
)
from control_plane.app.models import ExecutionRun, Task
from control_plane.app.opencode_client import OpenCodeError


def _seed_running_execution(
    session_id: str = "worker-session-1",
    *,
    deadline_at: datetime | None = None,
) -> tuple[str, str]:
    with SessionLocal() as db:
        task = Task(title="Проверить durable execution", domain="development")
        db.add(task)
        db.flush()
        task.status = "in_progress"
        run = ExecutionRun(
            task_id=task.id,
            opencode_session_id=session_id,
            deadline_at=deadline_at,
        )
        db.add(run)
        db.commit()
        return task.id, run.id


def _seed_queued_execution(
    session_id: str | None = None,
    *,
    deadline_at: datetime | None = None,
) -> tuple[str, str]:
    with SessionLocal() as db:
        task = Task(title="Проверить durable dispatch", domain="development")
        db.add(task)
        db.flush()
        task.status = "in_progress"
        run = ExecutionRun(
            task_id=task.id,
            status="queued",
            stage="dispatch_pending",
            opencode_session_id=session_id,
            assigned_roles=["department-lead"],
            deadline_at=deadline_at,
        )
        db.add(run)
        db.commit()
        return task.id, run.id


def _steal_lease(run_id: str) -> None:
    """Deterministically simulate another generation winning before an external POST."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        run.lease_owner = "new-generation-worker"
        run.lease_generation = int(run.lease_generation or 0) + 1
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=60)
        db.commit()


def _request_cancel(run_id: str) -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        run.cancel_requested_at = now
        run.stage = "cancel_requested"
        run.lease_generation = int(run.lease_generation or 0) + 1
        run.lease_owner = None
        run.lease_expires_at = None
        db.commit()


class FakeDispatchOpenCode:
    def __init__(
        self,
        *,
        sessions=None,
        existing_messages=None,
        fail_after_create=False,
        fail_abort=False,
    ):
        self.sessions = list(sessions or [])
        self.existing_messages = set(existing_messages or set())
        self.fail_after_create = fail_after_create
        self.fail_abort = fail_abort
        self.create_calls = 0
        self.prompt_calls: list[tuple[str, str, str]] = []
        self.abort_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.cleanup_calls: list[tuple[str, str]] = []

    def sessions_for_execution(self, execution_id: str):
        return [
            session
            for session in self.sessions
            if (session.get("metadata") or {}).get(EXECUTION_METADATA_KEY) == execution_id
        ]

    def create_session(self, title: str, *, metadata: dict | None = None):
        self.create_calls += 1
        session = {
            "id": f"created-session-{self.create_calls}",
            "title": title,
            "metadata": dict(metadata or {}),
        }
        self.sessions.append(session)
        if self.fail_after_create:
            self.fail_after_create = False
            raise OpenCodeError("simulated lost create-session response")
        return session

    def message(self, session_id: str, message_id: str):
        if (session_id, message_id) in self.existing_messages:
            return {"info": {"id": message_id, "role": "user"}, "parts": []}
        return None

    def prompt_async(
        self,
        session_id: str,
        prompt: str,
        *,
        message_id: str,
        part_id: str,
    ):
        assert "не выполняй production deploy" in prompt
        assert message_id.startswith("msg")
        assert part_id.startswith("prt")
        self.prompt_calls.append((session_id, message_id, part_id))
        self.existing_messages.add((session_id, message_id))

    def abort(self, session_id: str):
        self.abort_calls.append(session_id)
        self.cleanup_calls.append(("abort", session_id))
        if self.fail_abort:
            raise OpenCodeError("simulated abort uncertainty")

    def delete_session(self, session_id: str):
        self.delete_calls.append(session_id)
        self.cleanup_calls.append(("delete", session_id))
        if self.fail_abort:
            raise OpenCodeError("simulated delete uncertainty")


class StealLeaseBeforeCreate(FakeDispatchOpenCode):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def sessions_for_execution(self, execution_id: str):
        _steal_lease(self.run_id)
        return []


class StealLeaseBeforePrompt(FakeDispatchOpenCode):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def message(self, session_id: str, message_id: str):
        _steal_lease(self.run_id)
        return None


class CancelBeforePrompt(FakeDispatchOpenCode):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def message(self, session_id: str, message_id: str):
        _request_cancel(self.run_id)
        return None


@pytest.mark.parametrize("timeout_seconds", [59, 604801])
def test_execution_timeout_must_stay_within_supported_bounds(timeout_seconds):
    with pytest.raises(ValueError, match="between 60 and 604800"):
        ExecutionLeaseManager(
            "invalid-timeout",
            lease_seconds=60,
            execution_timeout_seconds=timeout_seconds,
        )


def test_heartbeat_keeps_live_lease_from_being_recovered():
    _, run_id = _seed_running_execution()
    t0 = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    worker_a = ExecutionLeaseManager("worker-a", lease_seconds=30)
    worker_b = ExecutionLeaseManager("worker-b", lease_seconds=30)

    with SessionLocal() as db:
        [lease_a] = worker_a.claim_available(db, limit=1, now=t0)
    assert lease_a.generation == 1
    assert lease_a.status == "running"

    with SessionLocal() as db:
        assert worker_a.heartbeat(db, lease_a, now=t0 + timedelta(seconds=20))

    with SessionLocal() as db:
        assert worker_b.claim_available(db, limit=1, now=t0 + timedelta(seconds=31)) == []
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.lease_owner == "worker-a"
        assert run.lease_generation == 1


def test_expired_lease_is_recovered_and_zombie_result_is_fenced():
    task_id, run_id = _seed_running_execution("worker-session-2")
    t0 = datetime(2026, 9, 5, 13, 0, tzinfo=timezone.utc)
    worker_a = ExecutionLeaseManager("worker-a", lease_seconds=30)
    worker_b = ExecutionLeaseManager("worker-b", lease_seconds=30)

    with SessionLocal() as db:
        [lease_a] = worker_a.claim_available(db, limit=1, now=t0)

    with SessionLocal() as db:
        assert (
            worker_a.apply_observation(
                db,
                lease_a,
                state_type="idle",
                result="stale result before recovery",
                now=t0 + timedelta(seconds=31),
            )
            == "lost"
        )

    with SessionLocal() as db:
        [lease_b] = worker_b.claim_available(
            db,
            limit=1,
            now=t0 + timedelta(seconds=31),
        )
    assert lease_b.generation == 2

    with SessionLocal() as db:
        assert (
            worker_a.apply_observation(
                db,
                lease_a,
                state_type="idle",
                result="zombie result",
                now=t0 + timedelta(seconds=32),
            )
            == "lost"
        )

    with SessionLocal() as db:
        assert (
            worker_b.apply_observation(
                db,
                lease_b,
                state_type="idle",
                result="authoritative result",
                now=t0 + timedelta(seconds=32),
            )
            == "completed"
        )

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        task = db.get(Task, task_id)
        assert run is not None and task is not None
        assert run.status == "completed"
        assert run.result == "authoritative result"
        assert run.lease_generation == 2
        assert run.lease_owner is None
        assert run.lease_expires_at is None
        assert task.status == "qa"


def test_same_generation_cannot_commit_after_lease_expiry():
    _, run_id = _seed_running_execution("worker-session-3")
    t0 = datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
    worker = ExecutionLeaseManager("worker-a", lease_seconds=30)

    with SessionLocal() as db:
        [lease] = worker.claim_available(db, limit=1, now=t0)

    with SessionLocal() as db:
        outcome = worker.apply_observation(
            db,
            lease,
            state_type="idle",
            result="too late",
            now=t0 + timedelta(seconds=30),
        )
    assert outcome == "lost"

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status == "running"
        assert run.result == ""


def test_dispatch_recovers_session_when_create_response_was_lost():
    _, run_id = _seed_queued_execution()
    manager = ExecutionLeaseManager("worker-dispatch", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)
    assert lease.status == "queued"

    fake = FakeDispatchOpenCode(fail_after_create=True)
    with pytest.raises(OpenCodeError):
        dispatch_execution(manager, fake, lease)

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status == "queued"
        assert run.opencode_session_id is None

    assert dispatch_execution(manager, fake, lease) == "dispatched"
    assert fake.create_calls == 1
    assert fake.prompt_calls == [
        (
            "created-session-1",
            execution_message_id(run_id),
            execution_part_id(run_id),
        )
    ]

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status == "running"
        assert run.stage == "department_lead"
        assert run.opencode_session_id == "created-session-1"
        assert run.lease_owner is None
        assert run.lease_expires_at is None


def test_dispatch_reconciles_existing_prompt_without_sending_duplicate():
    session_id = "session-after-accepted-prompt"
    _, run_id = _seed_queued_execution(session_id)
    message_id = execution_message_id(run_id)
    manager = ExecutionLeaseManager("worker-reconcile", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)

    fake = FakeDispatchOpenCode(existing_messages={(session_id, message_id)})
    assert dispatch_execution(manager, fake, lease) == "dispatched"
    assert fake.create_calls == 0
    assert fake.prompt_calls == []

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status == "running"
        assert run.opencode_session_id == session_id


def test_stale_dispatch_generation_cannot_persist_session_after_recovery():
    _, run_id = _seed_queued_execution()
    t0 = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)
    worker_a = ExecutionLeaseManager("dispatch-a", lease_seconds=30)
    worker_b = ExecutionLeaseManager("dispatch-b", lease_seconds=30)

    with SessionLocal() as db:
        [lease_a] = worker_a.claim_available(db, limit=1, now=t0)
    with SessionLocal() as db:
        [lease_b] = worker_b.claim_available(db, limit=1, now=t0 + timedelta(seconds=31))
    assert lease_b.generation == lease_a.generation + 1

    with SessionLocal() as db:
        assert not worker_a.persist_dispatch_session(
            db,
            lease_a,
            "zombie-session",
            now=t0 + timedelta(seconds=32),
        )

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status == "queued"
        assert run.opencode_session_id is None
        assert run.lease_owner == "dispatch-b"


def test_lost_generation_cannot_create_opencode_session():
    _, run_id = _seed_queued_execution()
    manager = ExecutionLeaseManager("stale-before-create", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)

    fake = StealLeaseBeforeCreate(run_id)
    assert dispatch_execution(manager, fake, lease) == "lost"
    assert fake.create_calls == 0
    assert fake.prompt_calls == []

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status == "queued"
        assert run.opencode_session_id is None
        assert run.lease_owner == "new-generation-worker"
        assert run.lease_generation == lease.generation + 1


def test_lost_generation_cannot_send_opencode_prompt():
    session_id = "session-before-fenced-prompt"
    _, run_id = _seed_queued_execution(session_id)
    manager = ExecutionLeaseManager("stale-before-prompt", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)

    fake = StealLeaseBeforePrompt(run_id)
    assert dispatch_execution(manager, fake, lease) == "lost"
    assert fake.create_calls == 0
    assert fake.prompt_calls == []

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status == "queued"
        assert run.opencode_session_id == session_id
        assert run.lease_owner == "new-generation-worker"
        assert run.lease_generation == lease.generation + 1


def test_cancel_request_fences_dispatch_before_prompt():
    session_id = "session-cancel-before-prompt"
    _, run_id = _seed_queued_execution(session_id)
    manager = ExecutionLeaseManager("cancel-race", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)

    fake = CancelBeforePrompt(run_id)
    assert dispatch_execution(manager, fake, lease) == "lost"
    assert fake.prompt_calls == []

    recovery = ExecutionLeaseManager("cancel-recovery", lease_seconds=60)
    with SessionLocal() as db:
        [cancel_lease] = recovery.claim_available(db, limit=1)
    assert cancel_lease.cancel_requested_at is not None
    assert cancel_execution(recovery, fake, cancel_lease) == "cancelled"
    assert fake.delete_calls == [session_id]
    assert fake.abort_calls == [session_id, session_id]
    assert fake.cleanup_calls == [
        ("abort", session_id),
        ("delete", session_id),
        ("abort", session_id),
    ]


def test_queued_cancellation_deletes_session_before_terminal_state():
    session_id = "queued-cancel-session"
    task_id, run_id = _seed_queued_execution(session_id)
    _request_cancel(run_id)
    manager = ExecutionLeaseManager("cancel-queued", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)

    fake = FakeDispatchOpenCode()
    assert poll_execution(manager, lambda: fake, lease) == "cancelled"
    assert fake.delete_calls == [session_id]
    assert fake.abort_calls == [session_id, session_id]
    assert fake.cleanup_calls == [
        ("abort", session_id),
        ("delete", session_id),
        ("abort", session_id),
    ]

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        task = db.get(Task, task_id)
        assert run is not None and task is not None
        assert run.status == "cancelled"
        assert run.stage == "stopped"
        assert run.finished_at is not None
        assert task.status == "failed"


def test_running_cancellation_retries_uncertain_abort():
    task_id, run_id = _seed_running_execution("running-cancel-session")
    _request_cancel(run_id)
    manager = ExecutionLeaseManager("cancel-running", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)

    fake = FakeDispatchOpenCode(fail_abort=True)
    assert cancel_execution(manager, fake, lease) == "running"
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        task = db.get(Task, task_id)
        assert run is not None and task is not None
        assert run.status == "running"
        assert run.stage == "cancel_cleanup_pending"
        assert run.lease_owner == "cancel-running"
        assert task.status == "in_progress"

    fake.fail_abort = False
    assert cancel_execution(manager, fake, lease) == "cancelled"
    assert fake.abort_calls == ["running-cancel-session", "running-cancel-session"]
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        task = db.get(Task, task_id)
        assert run is not None and task is not None
        assert run.status == "cancelled"
        assert task.status == "failed"


def test_overdue_queued_execution_fails_before_dispatch():
    now = datetime.now(timezone.utc)
    task_id, run_id = _seed_queued_execution(
        deadline_at=now - timedelta(seconds=1),
    )
    manager = ExecutionLeaseManager("timeout-queued", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1, now=now)

    fake = FakeDispatchOpenCode()
    assert poll_execution(manager, lambda: fake, lease) == "timed_out"
    assert fake.create_calls == 0
    assert fake.prompt_calls == []
    assert fake.abort_calls == []

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        task = db.get(Task, task_id)
        assert run is not None and task is not None
        assert run.status == "failed"
        assert run.stage == "timed_out"
        assert "durable deadline" in run.error
        assert run.finished_at is not None
        assert run.lease_owner is None
        assert task.status == "failed"


def test_timeout_waits_for_confirmed_abort_then_completes():
    now = datetime.now(timezone.utc)
    task_id, run_id = _seed_running_execution(
        "timeout-running-session",
        deadline_at=now - timedelta(seconds=1),
    )
    manager = ExecutionLeaseManager("timeout-running", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1, now=now)

    fake = FakeDispatchOpenCode(fail_abort=True)
    assert timeout_execution(manager, fake, lease) == "running"
    assert fake.abort_calls == ["timeout-running-session"]

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        task = db.get(Task, task_id)
        assert run is not None and task is not None
        assert run.status == "running"
        assert run.stage == "timeout_abort_pending"
        assert "not yet confirmed" in run.error
        assert run.lease_owner == "timeout-running"
        assert task.status == "in_progress"

    fake.fail_abort = False
    assert timeout_execution(manager, fake, lease) == "timed_out"
    assert fake.abort_calls == ["timeout-running-session", "timeout-running-session"]

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        task = db.get(Task, task_id)
        assert run is not None and task is not None
        assert run.status == "failed"
        assert run.stage == "timed_out"
        assert run.lease_owner is None
        assert task.status == "failed"


def test_timeout_reconciles_and_aborts_all_unpersisted_sessions():
    now = datetime.now(timezone.utc)
    _, run_id = _seed_queued_execution(deadline_at=now - timedelta(seconds=1))
    manager = ExecutionLeaseManager("timeout-recovery", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1, now=now)

    sessions = [
        {
            "id": "lost-session-a",
            "metadata": {EXECUTION_METADATA_KEY: run_id},
        },
        {
            "id": "lost-session-b",
            "metadata": {EXECUTION_METADATA_KEY: run_id},
        },
    ]
    fake = FakeDispatchOpenCode(sessions=sessions)
    assert timeout_execution(manager, fake, lease) == "timed_out"
    assert fake.abort_calls == ["lost-session-a", "lost-session-b"]


def test_stale_generation_cannot_abort_or_commit_timeout():
    now = datetime.now(timezone.utc)
    _, run_id = _seed_running_execution(
        "stale-timeout-session",
        deadline_at=now - timedelta(seconds=1),
    )
    manager = ExecutionLeaseManager("stale-timeout", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1, now=now)
    _steal_lease(run_id)

    fake = FakeDispatchOpenCode()
    assert timeout_execution(manager, fake, lease) == "lost"
    assert fake.abort_calls == []

    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status == "running"
        assert run.lease_owner == "new-generation-worker"


def test_worker_health_file_is_refreshed(tmp_path):
    health_path = tmp_path / "worker.heartbeat"
    write_worker_health(health_path)
    first_mtime = health_path.stat().st_mtime_ns
    write_worker_health(health_path)
    assert health_path.is_file()
    assert health_path.stat().st_mtime_ns >= first_mtime
