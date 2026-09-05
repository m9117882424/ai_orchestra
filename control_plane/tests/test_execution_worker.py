from datetime import datetime, timedelta, timezone

import pytest

from control_plane.app.db import SessionLocal
from control_plane.app.execution_protocol import (
    EXECUTION_METADATA_KEY,
    execution_message_id,
)
from control_plane.app.execution_worker import (
    ExecutionLeaseManager,
    dispatch_execution,
)
from control_plane.app.models import ExecutionRun, Task
from control_plane.app.opencode_client import OpenCodeError


def _seed_running_execution(session_id: str = "worker-session-1") -> tuple[str, str]:
    with SessionLocal() as db:
        task = Task(title="Проверить durable execution", domain="development")
        db.add(task)
        db.flush()
        task.status = "in_progress"
        run = ExecutionRun(task_id=task.id, opencode_session_id=session_id)
        db.add(run)
        db.commit()
        return task.id, run.id


def _seed_queued_execution(session_id: str | None = None) -> tuple[str, str]:
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


class FakeDispatchOpenCode:
    def __init__(self, *, sessions=None, existing_messages=None, fail_after_create=False):
        self.sessions = list(sessions or [])
        self.existing_messages = set(existing_messages or set())
        self.fail_after_create = fail_after_create
        self.create_calls = 0
        self.prompt_calls: list[tuple[str, str]] = []

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

    def prompt_async(self, session_id: str, prompt: str, *, message_id: str | None = None):
        assert "не выполняй production deploy" in prompt
        assert message_id is not None and message_id.startswith("msg")
        self.prompt_calls.append((session_id, message_id))
        self.existing_messages.add((session_id, message_id))


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
    assert len(fake.prompt_calls) == 1

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
