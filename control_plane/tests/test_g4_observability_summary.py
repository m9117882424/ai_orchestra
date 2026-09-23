from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from control_plane.app.db import SessionLocal
from control_plane.app.main import app
from control_plane.app.models import (
    AuditEvent,
    ExecutionRun,
    Repository,
    RunnerJob,
    Task,
    TaskWorkspace,
    UsageEvent,
)


def _seed_observability_risks() -> tuple[str, str, str]:
    now = datetime.now(timezone.utc)
    commit = "a" * 40
    digest = "b" * 64

    with SessionLocal() as db:
        repository = Repository(
            name="observability-fixture",
            remote_url="https://github.com/example/observability-fixture.git",
            remote_identity="https://github.com/example/observability-fixture",
            remote_host="github.com",
            provider="github",
        )
        db.add(repository)
        db.flush()

        task = Task(
            title="Observability fixture",
            status="in_progress",
            repository_id=repository.id,
        )
        db.add(task)
        db.flush()

        workspace = TaskWorkspace(
            task_id=task.id,
            repository_id=repository.id,
            status="pending",
            base_commit=commit,
            base_branch="main",
            branch_name="ai/task-observability",
            opencode_path="/workspace/worktrees/managed/observability-fixture",
            next_attempt_at=now,
            failure_count=2,
            last_error_code="workspace_transient_error",
            updated_at=now,
        )
        db.add(workspace)
        db.flush()

        run = ExecutionRun(
            task_id=task.id,
            contract_version=1,
            repository_id=repository.id,
            workspace_id=workspace.id,
            base_commit=commit,
            workspace_path=workspace.opencode_path,
            status="running",
            stage="runner_gate",
            deadline_at=now - timedelta(minutes=1),
            created_at=now - timedelta(minutes=10),
            updated_at=now,
        )
        db.add(run)
        db.flush()

        job = RunnerJob(
            execution_id=run.id,
            repository_id=repository.id,
            workspace_id=workspace.id,
            idempotency_key=str(uuid4()),
            status="queued",
            argv=["python3", "-c", "print('ok')"],
            timeout_seconds=30,
            base_commit=commit,
            preflight_digest=digest,
            cleanup_confirmed=None,
            next_attempt_at=now,
            failure_count=2,
            last_error_code="runner_transient_error",
            created_at=now - timedelta(minutes=5),
            updated_at=now,
        )
        db.add(job)

        db.add(
            UsageEvent(
                task_id=task.id,
                execution_id=run.id,
                role="department-lead",
                provider="test",
                model="known-cost",
                input_tokens=100,
                output_tokens=50,
                cost=Decimal("10000"),
            )
        )
        db.add(
            UsageEvent(
                task_id=task.id,
                execution_id=run.id,
                source="opencode-session",
                source_key="session-cost-unknown",
                role="qa-engineer",
                provider="test",
                model="unknown-cost",
                input_tokens=25,
                output_tokens=10,
                cost=Decimal("0"),
            )
        )
        db.commit()
        return run.id, job.id, workspace.id


def test_observability_summary_reports_only_durable_risks_and_is_read_only(auth):
    run_id, job_id, workspace_id = _seed_observability_risks()

    with SessionLocal() as db:
        audit_before = db.scalar(select(func.count(AuditEvent.id))) or 0

    with TestClient(app) as client:
        response = client.get("/api/observability/summary", auth=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["active_execution_count"] == 1
    assert body["active_runner_job_count"] == 1
    assert body["active_workspace_count"] == 1
    assert body["executions_by_status"]["running"] == 1
    assert body["runner_jobs_by_status"]["queued"] == 1
    assert body["workspaces_by_status"]["pending"] == 1
    assert Decimal(str(body["current_month_known_cost"])) == Decimal("10000")
    assert body["current_month_cost_status"] == "partial"
    assert body["unknown_automatic_cost_rows"] == 1

    alerts = {item["code"]: item for item in body["alerts"]}
    assert alerts["execution_deadline_exceeded"]["entity_id"] == run_id
    assert alerts["execution_deadline_exceeded"]["severity"] == "critical"
    assert alerts["runner_repeated_retry"]["entity_id"] == job_id
    assert alerts["workspace_repeated_retry"]["entity_id"] == workspace_id
    assert alerts["cost_telemetry_incomplete"]["severity"] == "warning"
    assert alerts["department_budget_warning"]["severity"] == "warning"
    assert body["alert_count"] >= 5
    assert body["alerts_truncated"] is False

    with SessionLocal() as db:
        audit_after = db.scalar(select(func.count(AuditEvent.id))) or 0
    assert audit_after == audit_before


def test_observability_summary_bounds_alert_payload(auth):
    _seed_observability_risks()

    with TestClient(app) as client:
        response = client.get("/api/observability/summary?alert_limit=2", auth=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["alert_count"] > 2
    assert body["alerts_truncated"] is True
    assert len(body["alerts"]) == 2
