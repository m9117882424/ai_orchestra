from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from control_plane.app.db import SessionLocal
from control_plane.app.main import app
from control_plane.app.models import ExecutionChildRun, ExecutionEvidence, ExecutionRun, Task


def test_operator_timeline_merges_lifecycle_evidence_and_child_runs(auth):
    created = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    started = created + timedelta(seconds=2)
    tool_at = created + timedelta(seconds=5)
    child_started = created + timedelta(seconds=7)
    child_finished = created + timedelta(seconds=11)
    finished = created + timedelta(seconds=15)

    with SessionLocal() as db:
        task = Task(title="Operator timeline", status="qa")
        db.add(task)
        db.flush()
        run = ExecutionRun(
            task_id=task.id,
            contract_version=1,
            status="completed",
            stage="qa",
            lead_role="department-lead",
            assigned_roles=["department-lead", "qa-engineer"],
            started_at=started,
            finished_at=finished,
            result="done",
            created_at=created,
            updated_at=finished,
        )
        db.add(run)
        db.flush()
        db.add(
            ExecutionEvidence(
                execution_id=run.id,
                source="opencode",
                source_key="tool-call-1",
                kind="tool",
                role="department-lead",
                tool_name="bash",
                status="completed",
                details={"summary": "tests executed"},
                occurred_at=tool_at,
            )
        )
        db.add(
            ExecutionChildRun(
                execution_id=run.id,
                source="opencode",
                source_run_id="ses-qa",
                parent_source_run_id="ses-root",
                parent_call_id="call-qa",
                role="qa-engineer",
                provider="orchestra",
                model="orchestra-qa",
                status="completed",
                attempt=1,
                started_at=child_started,
                finished_at=child_finished,
                last_observed_at=child_finished,
            )
        )
        db.commit()
        run_id = run.id

    with TestClient(app) as client:
        response = client.get(f"/api/executions/{run_id}/timeline", auth=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["execution_id"] == run_id
    labels = [item["label"] for item in body["items"]]
    assert labels == [
        "execution.created",
        "execution.started",
        "evidence.tool",
        "child_run.started",
        "child_run.finished",
        "execution.finished",
    ]
    tool = next(item for item in body["items"] if item["label"] == "evidence.tool")
    assert tool["details"]["tool_name"] == "bash"
    assert tool["details"]["summary"] == "tests executed"
    assert "RAW_INPUT_SECRET" not in str(body)


def test_operator_timeline_is_read_only_and_honors_limit(auth):
    created = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
    with SessionLocal() as db:
        task = Task(title="Timeline limit", status="in_progress")
        db.add(task)
        db.flush()
        run = ExecutionRun(
            task_id=task.id,
            contract_version=1,
            status="running",
            stage="department_lead",
            created_at=created,
            updated_at=created,
        )
        db.add(run)
        db.commit()
        run_id = run.id

    with TestClient(app) as client:
        response = client.get(f"/api/executions/{run_id}/timeline?limit=1", auth=auth)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id) is not None
