from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from control_plane.app.db import SessionLocal
from control_plane.app.evidence import (
    capture_opencode_evidence,
    materialize_result_package,
    materialize_terminal_result_package,
)
from control_plane.app.main import app
from control_plane.app.models import (
    ExecutionEvidence,
    ExecutionResultPackage,
    ExecutionRun,
    Repository,
    Task,
    TaskWorkspace,
    UsageEvent,
)


def _seed_running() -> tuple[str, str]:
    with SessionLocal() as db:
        task = Task(title="G4 evidence task", status="in_progress")
        db.add(task)
        db.flush()
        run = ExecutionRun(
            task_id=task.id,
            contract_version=1,
            status="running",
            stage="department_lead",
            opencode_session_id="ses-g4",
            lead_role="department-lead",
            assigned_roles=["department-lead", "qa"],
            lease_generation=3,
        )
        db.add(run)
        db.commit()
        return task.id, run.id


def test_opencode_evidence_capture_is_idempotent_and_redacts_tool_payload():
    _, run_id = _seed_running()
    messages = [
        {
            "info": {
                "id": "msg-1",
                "agent": "coder",
                "model": "orchestra-coder",
                "finish": "tool-calls",
                "time": {"created": "2026-09-17T08:00:00Z"},
            },
            "parts": [
                {"type": "text", "text": "Проверяю код."},
                {
                    "type": "tool",
                    "callID": "call-secret",
                    "tool": "bash",
                    "state": {
                        "status": "running",
                        "input": {"command": "echo SUPER_SECRET"},
                        "output": "SUPER_SECRET",
                        "time": {"start": 1789632000000},
                    },
                },
            ],
        }
    ]
    with SessionLocal() as db:
        first = capture_opencode_evidence(
            db,
            execution_id=run_id,
            generation=3,
            session_state="busy",
            messages=messages,
        )
        db.commit()
    with SessionLocal() as db:
        second = capture_opencode_evidence(
            db,
            execution_id=run_id,
            generation=3,
            session_state="busy",
            messages=messages,
        )
        db.commit()
        rows = list(
            db.scalars(
                select(ExecutionEvidence)
                .where(ExecutionEvidence.execution_id == run_id)
                .order_by(ExecutionEvidence.kind, ExecutionEvidence.source_key)
            )
        )

    assert first == 3
    assert second == 0
    assert {row.kind for row in rows} == {"stage", "message", "tool"}
    tool = next(row for row in rows if row.kind == "tool")
    assert tool.tool_name == "bash"
    assert tool.status == "running"
    assert "SUPER_SECRET" not in str(tool.details)
    assert set(tool.details) == {"call_id", "message_id"}


def test_final_result_package_is_content_addressed_and_immutable():
    task_id, run_id = _seed_running()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        run.status = "completed"
        run.stage = "manager_review"
        run.result = "Готово"
        run.finished_at = now
        db.add(
            UsageEvent(
                task_id=task_id,
                execution_id=run_id,
                role="department-lead",
                provider="test",
                model="orchestra-lead",
                input_tokens=100,
                output_tokens=40,
                cost=Decimal("0.125000"),
            )
        )
        package = materialize_result_package(db, run_id, final=True, now=now)
        digest = package.package_digest
        db.commit()

    with SessionLocal() as db:
        package = db.get(ExecutionResultPackage, run_id)
        assert package is not None
        assert package.state == "final"
        assert len(package.package_digest) == 64
        assert package.payload["original_task"]["title"] == "G4 evidence task"
        assert package.payload["execution"]["result"] == "Готово"
        assert package.payload["cost"] == {
            "input_tokens": 100,
            "output_tokens": 40,
            "actual_cost": "0.125000",
        }
        assert package.package_digest == digest
        db.add(
            UsageEvent(
                task_id=task_id,
                execution_id=run_id,
                role="qa",
                provider="test",
                model="orchestra-qa",
                input_tokens=1,
                output_tokens=1,
                cost=Decimal("0.001000"),
            )
        )
        db.flush()
        with pytest.raises(ValueError, match="final_result_package_is_immutable"):
            materialize_result_package(db, run_id, final=True, now=now)
        db.rollback()


def test_evidence_and_result_package_api_are_read_only(auth):
    _, run_id = _seed_running()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        run.status = "completed"
        run.result = "done"
        run.finished_at = now
        capture_opencode_evidence(
            db,
            execution_id=run_id,
            generation=3,
            session_state="idle",
            messages=[],
        )
        materialize_result_package(db, run_id, final=True, now=now)
        db.commit()

    with TestClient(app) as client:
        evidence = client.get(f"/api/executions/{run_id}/evidence", auth=auth)
        package = client.get(f"/api/executions/{run_id}/result-package", auth=auth)

    assert evidence.status_code == 200
    assert package.status_code == 200
    assert package.json()["state"] == "final"
    with SessionLocal() as db:
        assert db.get(ExecutionResultPackage, run_id) is not None


def test_failed_execution_without_workspace_gets_final_result_package():
    _, run_id = _seed_running()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run is not None
        run.status = "failed"
        run.stage = "runner_gate_failed"
        run.error = "runner evidence rejected"
        run.finished_at = now
        package = materialize_terminal_result_package(db, run, now=now)
        db.commit()

    assert package.state == "final"
    assert package.finalized_at is not None
    assert package.payload["execution"]["status"] == "failed"
    assert package.payload["execution"]["error"] == "runner evidence rejected"


def test_failed_execution_waiting_for_workspace_inspection_stays_provisional():
    now = datetime.now(timezone.utc)
    repository_id = "11111111-1111-4111-8111-111111111111"
    task_id = "22222222-2222-4222-8222-222222222222"
    workspace_id = "33333333-3333-4333-8333-333333333333"
    run_id = "44444444-4444-4444-8444-444444444444"
    path = f"/workspace/worktrees/managed/{workspace_id}"
    with SessionLocal() as db:
        db.add(
            Repository(
                id=repository_id,
                name="g4-provisional",
                remote_url="https://github.com/example/g4-provisional.git",
                remote_identity="github.com/example/g4-provisional",
                remote_host="github.com",
                provider="github",
            )
        )
        db.add(Task(id=task_id, title="G4 provisional", repository_id=repository_id, status="failed"))
        db.flush()
        db.add(
            TaskWorkspace(
                id=workspace_id,
                task_id=task_id,
                repository_id=repository_id,
                status="inspection_pending",
                base_commit="a" * 40,
                base_branch="main",
                branch_name="ai-orchestra/task-g4/run-provisional",
                opencode_path=path,
                initial_tree="b" * 40,
                preflight_digest="c" * 64,
                tracked_entries=1,
                current_head_commit="a" * 40,
                current_tree="b" * 40,
                inspection_requested_at=now,
                next_attempt_at=now,
                prepared_at=now,
            )
        )
        db.flush()
        run = ExecutionRun(
            id=run_id,
            task_id=task_id,
            contract_version=2,
            repository_id=repository_id,
            workspace_id=workspace_id,
            base_commit="a" * 40,
            workspace_path=path,
            workspace_tree="b" * 40,
            workspace_preflight_digest="c" * 64,
            workspace_preflight_completed_at=now,
            status="failed",
            stage="timed_out",
            error="deadline elapsed",
            finished_at=now,
        )
        db.add(run)
        db.flush()
        package = materialize_terminal_result_package(db, run, now=now)
        db.commit()

    assert package.state == "provisional"
    assert package.finalized_at is None
    assert "workspace inspection is not final" in package.payload["known_risks_limitations"]
