from datetime import datetime, timedelta, timezone

from control_plane.app.db import SessionLocal
from control_plane.app.execution_worker import ExecutionLeaseManager
from control_plane.app.models import ExecutionRun, Task


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


def test_heartbeat_keeps_live_lease_from_being_recovered():
    _, run_id = _seed_running_execution()
    t0 = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    worker_a = ExecutionLeaseManager("worker-a", lease_seconds=30)
    worker_b = ExecutionLeaseManager("worker-b", lease_seconds=30)

    with SessionLocal() as db:
        [lease_a] = worker_a.claim_available(db, limit=1, now=t0)
    assert lease_a.generation == 1

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
