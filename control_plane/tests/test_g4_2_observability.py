from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from control_plane.app.db import SessionLocal
from control_plane.app.evidence import materialize_result_package
from control_plane.app.main import app
from control_plane.app.models import (
    ExecutionChildRun,
    ExecutionEvidence,
    ExecutionRun,
    Task,
    UsageEvent,
)
from control_plane.app.opencode_observability import capture_opencode_observability


def _seed_running() -> tuple[str, str]:
    with SessionLocal() as db:
        task = Task(title="G4.2 telemetry", status="in_progress")
        db.add(task)
        db.flush()
        run = ExecutionRun(
            task_id=task.id,
            contract_version=1,
            status="running",
            stage="department_lead",
            opencode_session_id="ses-root",
            lead_role="department-lead",
            assigned_roles=["department-lead", "qa-engineer", "code-reviewer"],
            lease_generation=7,
        )
        db.add(run)
        db.commit()
        return task.id, run.id


def _session(session_id: str, agent: str, model: str, input_tokens: int, output_tokens: int, cost: str, *, parent: str | None = None):
    result = {
        "id": session_id,
        "agent": agent,
        "model": {"providerID": "orchestra", "id": model},
        "tokens": {"input": input_tokens, "output": output_tokens, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "cost": cost,
        "time": {"created": 1789632000000, "updated": 1789632060000},
    }
    if parent:
        result["parentID"] = parent
    return result


def _task_part(session_id: str, *, status: str, title: str, output: str, call_id: str, start: int, end: int):
    return {
        "type": "tool",
        "tool": "task",
        "callID": call_id,
        "state": {
            "status": status,
            "title": title,
            "output": output,
            "input": {"prompt": "RAW_INPUT_SECRET", "subagent_type": "qa-engineer"},
            "metadata": {
                "sessionId": session_id,
                "parentSessionId": "ses-root",
                "model": {"providerID": "orchestra", "modelID": "orchestra-qa"},
            },
            "time": {"start": start, "end": end},
        },
    }


def test_child_run_usage_and_structured_verdict_are_durable_and_redacted(auth):
    task_id, run_id = _seed_running()
    raw_output = (
        "RAW_OUTPUT_SECRET\n"
        '<AI_ORCHESTRA_VERDICT>{"version":1,"verdict":"pass","summary":"all checks passed",'
        '"findings":[{"severity":"low","summary":"minor residual","path":"app.py"}]}'
        "</AI_ORCHESTRA_VERDICT>"
    )
    messages = [{
        "info": {"id": "msg-lead", "agent": "department-lead", "role": "assistant", "time": {"created": 1789631999000}},
        "parts": [_task_part(
            "ses-qa", status="completed", title="Verify acceptance", output=raw_output,
            call_id="call-qa", start=1789632001000, end=1789632059000,
        )],
    }]
    sessions = [
        _session("ses-root", "department-lead", "orchestra-lead", 100, 20, "1.250000"),
        _session("ses-qa", "qa-engineer", "orchestra-qa", 50, 10, "0.500000", parent="ses-root"),
    ]

    for _ in range(2):
        with SessionLocal() as db:
            capture_opencode_observability(
                db,
                execution_id=run_id,
                generation=7,
                root_session_id="ses-root",
                session_state="busy",
                messages=messages,
                sessions=sessions,
            )
            db.commit()

    with SessionLocal() as db:
        children = list(db.scalars(select(ExecutionChildRun).where(ExecutionChildRun.execution_id == run_id)))
        usage = list(db.scalars(select(UsageEvent).where(UsageEvent.execution_id == run_id)))
        reviews = list(db.scalars(select(ExecutionEvidence).where(
            ExecutionEvidence.execution_id == run_id,
            ExecutionEvidence.kind == "review",
        )))
        assert len(children) == 1
        child = children[0]
        assert child.source_run_id == "ses-qa"
        assert child.parent_source_run_id == "ses-root"
        assert child.parent_call_id == "call-qa"
        assert child.role == "qa-engineer"
        assert child.provider == "orchestra"
        assert child.model == "orchestra-qa"
        assert child.status == "completed"
        assert child.attempt == 1 and child.retry_of_id is None
        assert len(usage) == 2
        assert {(row.source, row.source_key) for row in usage} == {
            ("opencode-session", "ses-root"),
            ("opencode-session", "ses-qa"),
        }
        assert sum(row.input_tokens for row in usage) == 150
        assert sum(row.output_tokens for row in usage) == 30
        assert sum(row.cost for row in usage) == Decimal("1.750000")
        assert len(reviews) == 1
        assert reviews[0].status == "pass"
        assert reviews[0].details["summary"] == "all checks passed"
        assert "RAW_OUTPUT_SECRET" not in str(reviews[0].details)
        assert "RAW_INPUT_SECRET" not in str(reviews[0].details)

        run = db.get(ExecutionRun, run_id)
        run.status = "completed"
        run.stage = "manager_review"
        run.finished_at = datetime.now(timezone.utc)
        package = materialize_result_package(db, run_id, final=True, now=run.finished_at)
        db.commit()
        assert package.package_version == 2
        assert package.payload["schema_version"] == 2
        assert package.payload["child_runs"][0]["role"] == "qa-engineer"
        assert package.payload["reviewer_qa_verdicts"][0]["status"] == "pass"
        assert package.payload["usage_capture"]["automatic_session_rows"] == 2
        assert package.payload["cost"] == {
            "input_tokens": 150,
            "output_tokens": 30,
            "actual_cost": "1.750000",
        }

    with TestClient(app) as client:
        response = client.get(f"/api/executions/{run_id}/child-runs", auth=auth)
    assert response.status_code == 200
    assert response.json()[0]["source_run_id"] == "ses-qa"


def test_retry_lineage_only_links_after_observed_failed_matching_child():
    _, run_id = _seed_running()
    first = _task_part(
        "ses-qa-1", status="error", title="Verify acceptance", output="failed",
        call_id="call-1", start=1789632001000, end=1789632010000,
    )
    second = _task_part(
        "ses-qa-2", status="completed", title="Verify acceptance", output="done",
        call_id="call-2", start=1789632020000, end=1789632030000,
    )
    sessions = [
        _session("ses-root", "department-lead", "orchestra-lead", 1, 1, "0"),
        _session("ses-qa-1", "qa-engineer", "orchestra-qa", 5, 2, "0", parent="ses-root"),
        _session("ses-qa-2", "qa-engineer", "orchestra-qa", 6, 3, "0", parent="ses-root"),
    ]
    messages = [{"info": {"id": "msg"}, "parts": [first, second]}]
    with SessionLocal() as db:
        capture_opencode_observability(
            db,
            execution_id=run_id,
            generation=7,
            root_session_id="ses-root",
            session_state="busy",
            messages=messages,
            sessions=sessions,
        )
        db.commit()

    with SessionLocal() as db:
        rows = list(db.scalars(
            select(ExecutionChildRun)
            .where(ExecutionChildRun.execution_id == run_id)
            .order_by(ExecutionChildRun.started_at.asc())
        ))
    assert len(rows) == 2
    assert rows[0].status == "failed"
    assert rows[0].attempt == 1 and rows[0].retry_of_id is None
    assert rows[1].status == "completed"
    assert rows[1].attempt == 2
    assert rows[1].retry_of_id == rows[0].id
