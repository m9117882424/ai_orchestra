from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from control_plane.app.db import SessionLocal
from control_plane.app.main import app
from control_plane.app.models import (
    CapabilityGuard,
    ControlledAction,
    ControlledActionAuthorization,
    ControlledActionEffect,
    ExecutionResultPackage,
    ExecutionRun,
    Repository,
    Task,
)


def _seed_ready_repository_task(
    *, assurance_tier: str = "general-standard"
) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        repository = Repository(
            name="g5-controlled-actions",
            remote_url="https://github.com/example/g5-controlled-actions.git",
            remote_identity="github.com/example/g5-controlled-actions",
            remote_host="github.com",
            provider="github",
            assurance_tier=assurance_tier,
            default_branch="main",
            enabled=True,
            status="ready",
            last_known_commit="a" * 40,
            last_fetched_at=now,
            sync_generation=1,
            sync_failure_count=0,
            sync_requested_at=now,
            sync_finished_at=now,
            sync_next_at=now + timedelta(hours=1),
        )
        db.add(repository)
        db.flush()
        task = Task(
            title="G5 exact action",
            status="waiting_approval",
            repository_id=repository.id,
        )
        db.add(task)
        db.commit()
        return repository.id, task.id


def _seed_final_result_package(
    repository_id: str,
    task_id: str,
    *,
    package_digest: str,
    provenance_status: str,
) -> str:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        run = ExecutionRun(
            task_id=task_id,
            contract_version=1,
            repository_id=repository_id,
            status="completed",
            stage="manager_review",
            result="done",
            finished_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(run)
        db.flush()
        db.add(
            ExecutionResultPackage(
                execution_id=run.id,
                package_version=2,
                state="final",
                payload={
                    "assurance": {
                        "tier": "general-high-assurance",
                        "provenance_required": True,
                        "provenance_status": provenance_status,
                        "missing_requirements": (
                            [] if provenance_status == "complete" else ["trusted_artifact_digests"]
                        ),
                    }
                },
                package_digest=package_digest,
                generated_at=now,
                finalized_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()
    return package_digest


def _action_payload(repository_id: str, task_id: str, *, action_type: str = "git_push") -> dict:
    return {
        "task_id": task_id,
        "repository_id": repository_id,
        "action_type": action_type,
        "source_sha": "a" * 40,
        "head_sha": "b" * 40,
        "destination": "refs/heads/feature/g5",
        "payload": {"z": 2, "a": 1},
    }


def _create_action(client: TestClient, auth, mutation_headers, *, action_type: str = "git_push") -> dict:
    repository_id, task_id = _seed_ready_repository_task()
    response = client.post(
        "/api/controlled-actions",
        auth=auth,
        headers=mutation_headers,
        json=_action_payload(repository_id, task_id, action_type=action_type),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _approve_action(client: TestClient, auth, mutation_headers, action: dict) -> dict:
    requested = client.post(
        f"/api/controlled-actions/{action['id']}/authorizations",
        auth=auth,
        headers=mutation_headers,
        json={
            "reason": "Exact digest authorization for controlled action",
            "ttl_seconds": 3600,
            "expected_action_digest": action["action_digest"],
        },
    )
    assert requested.status_code == 201, requested.text
    authorization = requested.json()
    decided = client.post(
        f"/api/controlled-action-authorizations/{authorization['id']}/decision",
        auth=auth,
        headers=mutation_headers,
        json={
            "decision": "approved",
            "expected_action_digest": action["action_digest"],
            "comment": "approved exact immutable action",
        },
    )
    assert decided.status_code == 200, decided.text
    return decided.json()


def test_exact_action_digest_is_unique_and_database_tamper_fails_closed(auth, mutation_headers):
    repository_id, task_id = _seed_ready_repository_task()
    payload = _action_payload(repository_id, task_id)
    with TestClient(app) as client:
        first = client.post(
            "/api/controlled-actions",
            auth=auth,
            headers=mutation_headers,
            json=payload,
        )
        duplicate_payload = {**payload, "payload": {"a": 1, "z": 2}}
        duplicate = client.post(
            "/api/controlled-actions",
            auth=auth,
            headers=mutation_headers,
            json=duplicate_payload,
        )

    assert first.status_code == 201
    action = first.json()
    assert len(action["action_digest"]) == 64
    assert action["status"] == "proposed"
    assert duplicate.status_code == 409

    with SessionLocal() as db:
        row = db.get(ControlledAction, action["id"])
        assert row is not None
        row.destination = "refs/heads/tampered"
        db.commit()

    with TestClient(app) as client:
        read = client.get(f"/api/controlled-actions/{action['id']}", auth=auth)
    assert read.status_code == 409
    assert read.json()["detail"] == "controlled_action_digest_mismatch"


def test_digest_bound_approval_and_capability_denial_do_not_consume(auth, mutation_headers):
    with TestClient(app) as client:
        action = _create_action(client, auth, mutation_headers)
        stale_request = client.post(
            f"/api/controlled-actions/{action['id']}/authorizations",
            auth=auth,
            headers=mutation_headers,
            json={
                "reason": "wrong digest",
                "ttl_seconds": 3600,
                "expected_action_digest": "f" * 64,
            },
        )
        assert stale_request.status_code == 409

        authorization = _approve_action(client, auth, mutation_headers, action)
        denied = client.post(
            f"/api/controlled-actions/{action['id']}/claim",
            auth=auth,
            headers=mutation_headers,
            json={
                "expected_action_digest": action["action_digest"],
            },
        )
        authorizations = client.get(
            f"/api/controlled-actions/{action['id']}/authorizations",
            auth=auth,
        )

    assert authorization["status"] == "approved"
    assert denied.status_code == 403
    [persisted] = authorizations.json()
    assert persisted["status"] == "approved"
    assert persisted["consumed_at"] is None
    assert persisted["operation_key"] is None


def test_approved_action_is_claimed_exactly_once_when_guard_is_enabled(auth, mutation_headers):
    with TestClient(app) as client:
        action = _create_action(client, auth, mutation_headers)
        authorization = _approve_action(client, auth, mutation_headers, action)

    with SessionLocal() as db:
        guard = db.get(CapabilityGuard, 1)
        assert guard is not None
        guard.external_write_allowed = True
        db.commit()

    with TestClient(app) as client:
        claimed = client.post(
            f"/api/controlled-actions/{action['id']}/claim",
            auth=auth,
            headers=mutation_headers,
            json={
                "expected_action_digest": action["action_digest"],
            },
        )
        operation_key = claimed.json()["operation_key"]
        replay = client.post(
            f"/api/controlled-actions/{action['id']}/claim",
            auth=auth,
            headers=mutation_headers,
            json={
                "expected_action_digest": action["action_digest"],
            },
        )

    assert claimed.status_code == 201, claimed.text
    effect = claimed.json()
    assert effect["status"] == "reserved"
    assert effect["operation_key"] == f"g5-{action['action_digest']}"
    assert effect["operation_key"] == operation_key
    assert replay.status_code == 409

    with SessionLocal() as db:
        stored_authorization = db.get(ControlledActionAuthorization, authorization["id"])
        stored_action = db.get(ControlledAction, action["id"])
        stored_effect = db.scalar(
            select(ControlledActionEffect).where(ControlledActionEffect.action_id == action["id"])
        )
        assert stored_authorization is not None
        assert stored_authorization.status == "consumed"
        assert stored_authorization.operation_key == operation_key
        assert stored_action is not None and stored_action.status == "claimed"
        assert stored_effect is not None and stored_effect.authorization_id == authorization["id"]


def test_expired_authorization_is_rejected_and_action_returns_to_proposed(auth, mutation_headers):
    with TestClient(app) as client:
        action = _create_action(client, auth, mutation_headers)
        requested = client.post(
            f"/api/controlled-actions/{action['id']}/authorizations",
            auth=auth,
            headers=mutation_headers,
            json={
                "reason": "short-lived exact approval",
                "ttl_seconds": 60,
                "expected_action_digest": action["action_digest"],
            },
        )
    authorization = requested.json()

    with SessionLocal() as db:
        row = db.get(ControlledActionAuthorization, authorization["id"])
        assert row is not None
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    with TestClient(app) as client:
        decision = client.post(
            f"/api/controlled-action-authorizations/{authorization['id']}/decision",
            auth=auth,
            headers=mutation_headers,
            json={
                "decision": "approved",
                "expected_action_digest": action["action_digest"],
                "comment": "too late",
            },
        )
        action_read = client.get(f"/api/controlled-actions/{action['id']}", auth=auth)
        auth_read = client.get(
            f"/api/controlled-actions/{action['id']}/authorizations",
            auth=auth,
        )

    assert decision.status_code == 409
    assert action_read.json()["status"] == "proposed"
    assert auth_read.json()[0]["status"] == "expired"


def test_high_assurance_action_requires_complete_final_result_package(auth, mutation_headers):
    repository_id, task_id = _seed_ready_repository_task(
        assurance_tier="general-high-assurance"
    )
    base = _action_payload(repository_id, task_id)

    with TestClient(app) as client:
        missing = client.post(
            "/api/controlled-actions",
            auth=auth,
            headers=mutation_headers,
            json=base,
        )
    assert missing.status_code == 409

    incomplete_digest = _seed_final_result_package(
        repository_id,
        task_id,
        package_digest="c" * 64,
        provenance_status="incomplete",
    )
    with TestClient(app) as client:
        incomplete = client.post(
            "/api/controlled-actions",
            auth=auth,
            headers=mutation_headers,
            json={**base, "result_package_digest": incomplete_digest},
        )
    assert incomplete.status_code == 409
    assert "provenance incomplete" in incomplete.json()["detail"]

    complete_digest = _seed_final_result_package(
        repository_id,
        task_id,
        package_digest="d" * 64,
        provenance_status="complete",
    )
    with TestClient(app) as client:
        accepted = client.post(
            "/api/controlled-actions",
            auth=auth,
            headers=mutation_headers,
            json={**base, "result_package_digest": complete_digest},
        )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["result_package_digest"] == complete_digest


def test_deploy_uses_separate_production_capability(auth, mutation_headers):
    with TestClient(app) as client:
        action = _create_action(client, auth, mutation_headers, action_type="deploy")
        _approve_action(client, auth, mutation_headers, action)

    with SessionLocal() as db:
        guard = db.get(CapabilityGuard, 1)
        assert guard is not None
        guard.external_write_allowed = True
        guard.production_deploy_allowed = False
        db.commit()

    with TestClient(app) as client:
        denied = client.post(
            f"/api/controlled-actions/{action['id']}/claim",
            auth=auth,
            headers=mutation_headers,
            json={
                "expected_action_digest": action["action_digest"],
            },
        )
    assert denied.status_code == 403
    assert "production_deploy_allowed" in denied.json()["detail"]


def test_reconciliation_ledger_requires_exact_source_and_desired_digests(auth, mutation_headers):
    with TestClient(app) as client:
        action = _create_action(client, auth, mutation_headers)
        _approve_action(client, auth, mutation_headers, action)

    with SessionLocal() as db:
        guard = db.get(CapabilityGuard, 1)
        assert guard is not None
        guard.external_write_allowed = True
        db.commit()

    with TestClient(app) as client:
        claimed = client.post(
            f"/api/controlled-actions/{action['id']}/claim",
            auth=auth,
            headers=mutation_headers,
            json={
                "expected_action_digest": action["action_digest"],
            },
        )
        effect = claimed.json()
        operation_key = effect["operation_key"]
        wrong_source = client.post(
            f"/api/controlled-action-effects/{effect['id']}/reconciliation",
            auth=auth,
            headers=mutation_headers,
            json={
                "operation_key": operation_key,
                "outcome": "source_state",
                "observed_state_digest": "c" * 40,
            },
        )
        source_ok = client.post(
            f"/api/controlled-action-effects/{effect['id']}/reconciliation",
            auth=auth,
            headers=mutation_headers,
            json={
                "operation_key": operation_key,
                "outcome": "source_state",
                "observed_state_digest": action["source_sha"],
                "external_ref": "github:refs/heads/feature/g5",
            },
        )
        desired_ok = client.post(
            f"/api/controlled-action-effects/{effect['id']}/reconciliation",
            auth=auth,
            headers=mutation_headers,
            json={
                "operation_key": operation_key,
                "outcome": "desired_state",
                "observed_state_digest": action["head_sha"],
                "external_ref": "github:refs/heads/feature/g5",
            },
        )
        action_read = client.get(f"/api/controlled-actions/{action['id']}", auth=auth)

    assert wrong_source.status_code == 409
    assert source_ok.status_code == 200
    assert source_ok.json()["status"] == "reserved"
    assert source_ok.json()["observed_before_digest"] == action["source_sha"]
    assert source_ok.json()["preflight_reconciled_at"] is not None
    assert desired_ok.status_code == 200
    assert desired_ok.json()["status"] == "reconciled"
    assert desired_ok.json()["result_digest"] == action["head_sha"]
    assert action_read.json()["status"] == "reconciled"
