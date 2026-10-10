"""Isolated database and HTTP checks for the scoped subscription lane."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from control_plane.app import codex_broker
from control_plane.app.db import SessionLocal
from control_plane.app.execution_worker import ExecutionLeaseManager
from control_plane.app.main import app
from control_plane.app.models import ExecutionChildRun, ExecutionResultPackage, ExecutionRun, Repository, Task, TaskWorkspace, new_id
from control_plane.app.workspace_protocol import workspace_path_for


def seed(status="queued", backend="codex-subscription", *, deadline=None, lease_expiry=None):
    now = datetime.now(timezone.utc)
    task_id, repository_id, workspace_id, run_id = (new_id() for _ in range(4))
    with SessionLocal() as db:
        db.add(Repository(id=repository_id, name=repository_id, remote_url="https://github.com/a/b.git",
                          remote_identity=repository_id, remote_host="github.com", provider="github",
                          enabled=True, status="ready", default_branch="main", last_known_commit="a" * 40,
                          last_fetched_at=now, sync_finished_at=now, sync_next_at=now))
        db.add(Task(id=task_id, title="Read-only design", description="Review code", repository_id=repository_id,
                    status="in_progress"))
        db.add(TaskWorkspace(id=workspace_id, task_id=task_id, repository_id=repository_id,
                             status="ready", base_commit="a" * 40, base_branch="main",
                             branch_name="ai-orchestra/test", opencode_path=workspace_path_for(workspace_id),
                             initial_tree="b" * 40, preflight_digest="c" * 64, tracked_entries=1,
                             prepared_at=now))
        db.flush()
        db.add(ExecutionRun(id=run_id, task_id=task_id, contract_version=2, repository_id=repository_id,
                            workspace_id=workspace_id, base_commit="a" * 40,
                            workspace_path=workspace_path_for(workspace_id), workspace_tree="b" * 40,
                            workspace_preflight_digest="c" * 64, workspace_preflight_completed_at=now,
                            workspace_runtime_preflight_digest="c" * 64 if status == "running" else None,
                            workspace_runtime_verified_at=now if status == "running" else None,
                            backend=backend, status=status, stage="architect_running" if status == "running" else "dispatch_pending",
                            lease_owner="codex-worker" if status == "running" else None,
                            lease_generation=1 if status == "running" else 0,
                            lease_expires_at=lease_expiry or (now + timedelta(seconds=120) if status == "running" else None),
                            deadline_at=deadline or now + timedelta(hours=1)))
        db.commit()
    return run_id


@pytest.fixture
def broker_client(tmp_path, monkeypatch):
    token = tmp_path / "worker.token"
    token.write_text("x" * 48)
    token.chmod(0o600)
    monkeypatch.setattr(codex_broker, "get_settings", lambda: SimpleNamespace(codex_worker_token_file=str(token)))
    with TestClient(app) as client:
        yield client, {"Authorization": "Bearer " + "x" * 48}, token


def result(generation=1, role="architect", outcome="completed", text="Independent report"):
    return {"generation": generation, "role": role, "outcome": outcome,
            "model": "gpt-6-astra", "final_text": text,
            "final_sha256": sha256(text.encode()).hexdigest(),
            "events_sha256": "d" * 64, "artifact_path": "/tmp/isolated/result",
            "base_commit": "a" * 40, "base_tree": "b" * 40,
            "workspace_preflight_digest": "c" * 64}


def preflight_result(generation=1):
    return {"generation": generation, "base_commit": "a" * 40,
            "base_tree": "b" * 40, "workspace_preflight_digest": "c" * 64}



def test_manager_abort_unclaimed_subscription_is_terminal_and_idempotent(
        broker_client, auth, mutation_headers):
    client, worker_headers, _ = broker_client
    run_id = seed()
    first = client.post(f"/api/executions/{run_id}/abort",
                        auth=auth, headers=mutation_headers)
    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"
    assert first.json()["stage"] == "codex_cancelled"
    assert first.json()["cancel_requested_at"] is not None
    second = client.post(f"/api/executions/{run_id}/abort",
                         auth=auth, headers=mutation_headers)
    assert second.status_code == 200
    assert second.json()["lease_generation"] == first.json()["lease_generation"]
    assert client.post("/api/codex-worker/claim", headers=worker_headers).json() is None
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "cancelled"
        assert db.query(ExecutionResultPackage).filter_by(execution_id=run_id).count() == 1


def test_manager_abort_preflight_pending_fences_without_model(
        broker_client, auth, mutation_headers):
    client, headers, _ = broker_client
    run_id = seed()
    claimed = client.post("/api/codex-worker/claim", headers=headers).json()
    assert claimed["execution_id"] == run_id
    aborted = client.post(f"/api/executions/{run_id}/abort",
                          auth=auth, headers=mutation_headers)
    assert aborted.status_code == 200
    assert aborted.json()["status"] == "cancelled"
    assert client.post(f"/api/codex-worker/{run_id}/preflight",
                       headers=headers, json=preflight_result()).status_code == 409
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None


def test_manager_abort_running_requires_bounded_reconciliation(
        broker_client, auth, mutation_headers):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    first = client.post(f"/api/executions/{run_id}/abort",
                        auth=auth, headers=mutation_headers)
    assert first.status_code == 200
    assert first.json()["status"] == "running"
    assert first.json()["stage"] == "cancel_requested"
    assert first.json()["lease_generation"] == 2
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).lease_owner == "codex-worker"
    assert first.json()["cancel_requested_at"] is not None
    again = client.post(f"/api/executions/{run_id}/abort",
                        auth=auth, headers=mutation_headers)
    assert again.status_code == 200
    assert again.json()["lease_generation"] == 2
    assert client.post(f"/api/codex-worker/{run_id}/heartbeat",
                       headers=headers, json={"generation": 1}).status_code == 409
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "running"
        assert run.lease_expires_at is not None
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "cancelled"
        assert run.stage == "codex_cancelled"
        assert db.query(ExecutionResultPackage).filter_by(execution_id=run_id).count() == 1




def test_subscription_execution_never_masquerades_as_zero_monthly_cost(
        broker_client):
    from control_plane.app.services import current_month_cost_summary
    run_id = seed()
    with SessionLocal() as db:
        summary = current_month_cost_summary(db)
        assert summary["cost_status"] == "unknown"
        assert summary["known_cost"] == 0
        assert summary["unknown_subscription_executions"] == 1
        assert summary["unknown_automatic_cost_rows"] >= 1
        assert db.get(ExecutionRun, run_id).backend == "codex-subscription"

def test_claim_requires_scoped_token(broker_client):
    client, headers, _ = broker_client
    assert client.post("/api/codex-worker/claim").status_code == 401
    assert client.post("/api/codex-worker/claim", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_manager_basic_cannot_claim(broker_client, auth):
    client, _, _ = broker_client
    assert client.post("/api/codex-worker/claim", auth=auth).status_code == 401


def test_token_permissions_fail_closed(broker_client):
    client, headers, token = broker_client
    token.chmod(0o644)
    assert client.post("/api/codex-worker/claim", headers=headers).status_code == 503


def test_invalid_token_content_and_symlink_fail_closed(broker_client, tmp_path):
    client, headers, token = broker_client
    token.write_text("short")
    assert client.post("/api/codex-worker/claim", headers=headers).status_code == 503
    token.write_text("x" * 48)
    other = tmp_path / "other.token"
    token.rename(other)
    token.symlink_to(other)
    assert client.post("/api/codex-worker/claim", headers=headers).status_code == 503


def test_claim_is_single_use_and_fenced(broker_client):
    client, headers, _ = broker_client
    run_id = seed()
    first = client.post("/api/codex-worker/claim", headers=headers)
    assert first.status_code == 200 and first.json()["execution_id"] == run_id
    assert first.json()["generation"] == 1
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "queued" and run.stage == "codex_preflight_pending"
        assert run.workspace_runtime_verified_at is None
        assert run.workspace_runtime_preflight_digest is None
    assert client.post(f"/api/codex-worker/{run_id}/phase", headers=headers,
                       json=result()).status_code == 409
    response = client.post(f"/api/codex-worker/{run_id}/preflight", headers=headers,
                           json=preflight_result())
    assert response.status_code == 200 and response.json()["stage"] == "architect_running"
    assert client.post(f"/api/codex-worker/{run_id}/preflight", headers=headers,
                       json=preflight_result()).status_code == 409
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "running" and run.workspace_runtime_verified_at is not None


def test_control_plane_claim_and_phase_never_touch_git_or_workspace(broker_client, monkeypatch):
    client, headers, _ = broker_client
    run_id = seed()
    def forbidden(*args, **kwargs):
        raise AssertionError("Control Plane accessed the workspace or Git")
    monkeypatch.setattr("control_plane.app.workspace_protocol.verify_runtime_workspace", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    assert client.post("/api/codex-worker/claim", headers=headers).json()["execution_id"] == run_id
    assert client.post(f"/api/codex-worker/{run_id}/preflight", headers=headers,
                       json=preflight_result()).status_code == 200
    assert client.post(f"/api/codex-worker/{run_id}/phase", headers=headers,
                       json=result()).json()["stage"] == "qa_running"
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).workspace_runtime_preflight_digest == "c" * 64


def test_preflight_rejects_stale_or_changed_binding(broker_client):
    client, headers, _ = broker_client
    run_id = seed()
    assert client.post("/api/codex-worker/claim", headers=headers).status_code == 200
    url = f"/api/codex-worker/{run_id}/preflight"
    assert client.post(url, headers=headers, json=preflight_result(2)).status_code == 409
    for field, value in (("base_commit", "f" * 40), ("base_tree", "f" * 40),
                         ("workspace_preflight_digest", "f" * 64)):
        payload = preflight_result()
        payload[field] = value
        assert client.post(url, headers=headers, json=payload).status_code == 409
    payload = preflight_result()
    payload["preflight_digest"] = "c" * 64
    assert client.post(url, headers=headers, json=payload).status_code == 422
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        db.get(TaskWorkspace, run.workspace_id).initial_tree = "f" * 40
        db.commit()
    assert client.post(url, headers=headers, json=preflight_result()).status_code == 409
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "queued" and run.workspace_runtime_verified_at is None


def test_pending_heartbeat_and_expiry_are_fenced(broker_client):
    client, headers, _ = broker_client
    run_id = seed()
    assert client.post("/api/codex-worker/claim", headers=headers).status_code == 200
    heartbeat = f"/api/codex-worker/{run_id}/heartbeat"
    assert client.post(heartbeat, headers=headers, json={"generation": 2}).status_code == 409
    assert client.post(heartbeat, headers=headers, json={"generation": 1}).json()["stage"] == "codex_preflight_pending"
    with SessionLocal() as db:
        db.get(ExecutionRun, run_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.post(f"/api/codex-worker/{run_id}/preflight", headers=headers,
                       json=preflight_result()).status_code == 409
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "failed" and run.lease_generation == 2
        assert run.workspace_runtime_verified_at is None


def test_preflight_rejects_revoked_repository_without_paid_fallback(broker_client):
    client, headers, _ = broker_client
    run_id = seed()
    assert client.post("/api/codex-worker/claim", headers=headers).status_code == 200
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        repository = db.get(Repository, run.repository_id)
        repository.status = "unavailable"
        repository.enabled = False
        db.commit()
    assert client.post(f"/api/codex-worker/{run_id}/preflight", headers=headers,
                       json=preflight_result()).status_code == 409
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.backend == "codex-subscription"
        assert run.status == "queued" and run.workspace_runtime_verified_at is None


def test_opencode_worker_never_claims_subscription(broker_client):
    run_id = seed()
    with SessionLocal() as db:
        assert ExecutionLeaseManager("test").claim_available(db, limit=10) == []
        assert db.get(ExecutionRun, run_id).status == "queued"


def test_stale_generation_rejected(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    response = client.post(f"/api/codex-worker/{run_id}/heartbeat", headers=headers, json={"generation": 2})
    assert response.status_code == 409


def test_expired_running_run_fails_without_reclaim(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running", lease_expiry=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "failed" and run.lease_generation == 2


def test_expired_queued_run_never_invokes_model(broker_client):
    client, headers, _ = broker_client
    run_id = seed(deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).status == "failed"


def test_cancelled_queued_run_never_claimed(broker_client):
    client, headers, _ = broker_client
    run_id = seed()
    with SessionLocal() as db:
        db.get(ExecutionRun, run_id).cancel_requested_at = datetime.now(timezone.utc)
        db.commit()
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).status == "cancelled"


def test_role_sequence_and_explicit_reviewer(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    url = f"/api/codex-worker/{run_id}/phase"
    assert client.post(url, headers=headers, json=result(role="reviewer")).status_code == 409
    assert client.post(url, headers=headers, json=result()).json()["stage"] == "qa_running"
    assert client.post(url, headers=headers, json=result(role="qa")).json()["stage"] == "reviewer_running"
    assert client.post(url, headers=headers, json=result(role="reviewer")).json()["status"] == "completed"
    with SessionLocal() as db:
        assert db.query(ExecutionChildRun).filter_by(execution_id=run_id).count() == 3
        assert db.get(ExecutionRun, run_id).stage == "reviewer_completed"
        package = db.get(ExecutionResultPackage, run_id)
        assert package.payload["cost"]["cost_status"] == "unknown"
        assert len(package.payload["subscription_artifacts"]) == 3
        assert db.get(TaskWorkspace, db.get(ExecutionRun, run_id).workspace_id).status == "inspection_pending"


def test_model_failure_never_marks_done(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    response = client.post(f"/api/codex-worker/{run_id}/phase", headers=headers,
                           json=result(outcome="execution_failed", text=""))
    assert response.json()["status"] == "failed"
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).status == "failed"


def test_missing_result_and_bad_checksum_rejected(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    url = f"/api/codex-worker/{run_id}/phase"
    assert client.post(url, headers=headers, json=result(text="")).status_code == 422
    bad = result()
    bad["final_sha256"] = "e" * 64
    assert client.post(url, headers=headers, json=bad).status_code == 422


def test_read_only_violation_fails(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    response = client.post(f"/api/codex-worker/{run_id}/phase", headers=headers,
                           json=result(outcome="read_only_violation", text=""))
    assert response.json()["status"] == "failed"


def test_workspace_attestation_mismatch_rejected(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    payload = result()
    payload["workspace_preflight_digest"] = "f" * 64
    response = client.post(f"/api/codex-worker/{run_id}/phase", headers=headers, json=payload)
    assert response.status_code == 409


def test_phase_rejects_extra_digest_alias_and_changed_database_binding(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    url = f"/api/codex-worker/{run_id}/phase"
    payload = result()
    missing = result()
    del missing["workspace_preflight_digest"]
    assert client.post(url, headers=headers, json=missing).status_code == 422
    payload["preflight_digest"] = "f" * 64
    assert client.post(url, headers=headers, json=payload).status_code == 422
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        db.get(TaskWorkspace, run.workspace_id).preflight_digest = "f" * 64
        db.commit()
    assert client.post(url, headers=headers, json=result()).status_code == 409
    with SessionLocal() as db:
        assert db.query(ExecutionChildRun).filter_by(execution_id=run_id).count() == 0


def test_long_unicode_report_and_duplicate_role(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    url = f"/api/codex-worker/{run_id}/phase"
    response = client.post(url, headers=headers, json=result(text="Ж" * 60000))
    assert response.status_code == 200 and response.json()["stage"] == "qa_running"
    assert client.post(url, headers=headers, json=result()).status_code == 409


def test_oversize_report_rejected_without_advancing_role(broker_client):
    client, headers, _ = broker_client
    run_id = seed(status="running")
    url = f"/api/codex-worker/{run_id}/phase"
    assert client.post(url, headers=headers, json=result(text="x" * 100001)).status_code == 422
    assert client.post(url, headers=headers, json=result(text="💡" * 100000)).status_code == 200


def test_repository_revocation_blocks_claim(broker_client):
    client, headers, _ = broker_client
    run_id = seed()
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        repository = db.get(Repository, run.repository_id)
        repository.status = "unavailable"
        repository.enabled = False
        db.commit()
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).stage == "repository_trust_revoked"


def test_high_assurance_cannot_bypass_provenance(broker_client):
    client, headers, _ = broker_client
    run_id = seed()
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        db.get(Repository, run.repository_id).assurance_tier = "general-high-assurance"
        db.commit()
    assert client.post("/api/codex-worker/claim", headers=headers).json() is None
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).status == "failed"
