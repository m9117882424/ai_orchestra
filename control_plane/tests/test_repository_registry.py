from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from control_plane.app.db import SessionLocal
from control_plane.app.main import app
from control_plane.app.models import AuditEvent, Repository


def _register(client: TestClient, auth, mutation_headers, **overrides):
    payload = {
        "name": "ai-orchestra",
        "remote_url": "https://github.com/m9117882424/ai_orchestra.git",
        "auth_profile_ref": "git-readonly-primary",
        "enabled": True,
        "execution_profile": "development",
        "assurance_tier": "general-standard",
    }
    payload.update(overrides)
    return client.post(
        "/api/repositories",
        auth=auth,
        headers=mutation_headers,
        json=payload,
    )


def test_dashboard_exposes_repository_registry_without_credentials(auth):
    with TestClient(app) as client:
        response = client.get("/", auth=auth)

    assert response.status_code == 200
    assert "G2 Trusted Repo Manager" in response.text
    assert "Credentials сюда не вводятся" in response.text
    assert "password" not in response.text.lower()
    assert "token" not in response.text.lower()


def test_repository_registration_is_canonical_pending_and_audited(auth, mutation_headers):
    with TestClient(app) as client:
        response = _register(
            client,
            auth,
            mutation_headers,
            name="  AI_Orchestra  ",
            remote_url="HTTPS://GitHub.COM/m9117882424/AI_Orchestra.git/",
            auth_profile_ref="  GIT-READONLY.PRIMARY  ",
        )
        audit = client.get("/api/audit", auth=auth)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["name"] == "ai_orchestra"
    assert payload["remote_url"] == "https://github.com/m9117882424/AI_Orchestra.git"
    assert payload["remote_host"] == "github.com"
    assert payload["provider"] == "github"
    assert payload["auth_profile_ref"] == "git-readonly.primary"
    assert payload["status"] == "pending_validation"
    assert payload["default_branch"] is None
    assert payload["last_known_commit"] is None
    assert payload["last_fetched_at"] is None
    assert payload["version"] == 1

    with SessionLocal() as db:
        repository = db.get(Repository, payload["id"])
        assert repository is not None
        assert repository.remote_identity == "github.com/m9117882424/ai_orchestra"

    event = audit.json()[0]
    assert event["action"] == "repository.registered"
    assert event["entity_id"] == payload["id"]
    assert event["details"]["remote_host"] == "github.com"
    assert "git-readonly.primary" not in str(event["details"])
    assert payload["remote_url"] not in str(event["details"])


@pytest.mark.parametrize(
    "remote_url",
    [
        "http://github.com/owner/repository",
        "ssh://git@github.com/owner/repository.git",
        "git@github.com:owner/repository.git",
        "https://ghp_not-a-real-token@github.com/owner/repository.git",
        "https://github.com/owner/repository.git?token=secret",
        "https://github.com/owner/repository.git#main",
        "https://127.0.0.1/owner/repository.git",
        "https://[::1]/owner/repository.git",
        "https://localhost/owner/repository.git",
        "https://git.internal/owner/repository.git",
        "https://github.com:8443/owner/repository.git",
        "https://github.com/owner/%2e%2e/repository.git",
        "https://github.com/owner/../repository.git",
        "https://github.com/repository.git",
    ],
)
def test_repository_registration_rejects_ambiguous_or_unsafe_remote(
    auth,
    mutation_headers,
    remote_url,
):
    with TestClient(app) as client:
        response = _register(
            client,
            auth,
            mutation_headers,
            remote_url=remote_url,
        )

    assert response.status_code == 422


def test_repository_registration_rejects_duplicate_name_and_remote_identity(
    auth,
    mutation_headers,
):
    with TestClient(app) as client:
        first = _register(client, auth, mutation_headers)
        duplicate_name = _register(
            client,
            auth,
            mutation_headers,
            remote_url="https://git.company.com/platform/another-repository.git",
        )
        duplicate_remote = _register(
            client,
            auth,
            mutation_headers,
            name="orchestra-alias",
            remote_url="https://github.com/M9117882424/AI_ORCHESTRA",
        )

    assert first.status_code == 201
    assert duplicate_name.status_code == 409
    assert duplicate_remote.status_code == 409


def test_repository_operational_state_cannot_be_forged_by_manager(auth, mutation_headers):
    with TestClient(app) as client:
        created = _register(
            client,
            auth,
            mutation_headers,
            status="ready",
            provider="generic",
            last_known_commit="a" * 40,
        )

    assert created.status_code == 422
    errors = {tuple(item["loc"]) for item in created.json()["detail"]}
    assert ("body", "status") in errors
    assert ("body", "provider") in errors
    assert ("body", "last_known_commit") in errors


def test_repository_control_fields_require_exact_json_types(auth, mutation_headers):
    with TestClient(app) as client:
        string_boolean = _register(
            client,
            auth,
            mutation_headers,
            enabled="true",
        )
        created = _register(
            client,
            auth,
            mutation_headers,
            name="strict-update",
            remote_url="https://github.com/owner/strict-update.git",
        )
        boolean_version = client.patch(
            f"/api/repositories/{created.json()['id']}",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": True, "enabled": False},
        )

    assert string_boolean.status_code == 422
    assert created.status_code == 201
    assert boolean_version.status_code == 422


@pytest.mark.parametrize(
    "secret_like_reference",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "github_pat_abcdefghijklmnopqrstuvwxyz",
        "glpat-abcdefghijklmnopqrstuvwxyz",
        "sk-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_repository_rejects_secret_like_auth_profile_reference(
    auth,
    mutation_headers,
    secret_like_reference,
):
    with TestClient(app) as client:
        response = _register(
            client,
            auth,
            mutation_headers,
            auth_profile_ref=secret_like_reference,
        )

    assert response.status_code == 422


def test_repository_update_is_versioned_idempotent_and_redacts_auth_reference(
    auth,
    mutation_headers,
):
    with TestClient(app) as client:
        created = _register(client, auth, mutation_headers).json()
        changed = client.patch(
            f"/api/repositories/{created['id']}",
            auth=auth,
            headers=mutation_headers,
            json={
                "expected_version": 1,
                "auth_profile_ref": "git-readonly-secondary",
                "assurance_tier": "general-high-assurance",
            },
        )
        unchanged = client.patch(
            f"/api/repositories/{created['id']}",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": 2, "enabled": True},
        )
        stale = client.patch(
            f"/api/repositories/{created['id']}",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": 1, "enabled": False},
        )
        current = client.get(f"/api/repositories/{created['id']}", auth=auth)
        audit = client.get("/api/audit", auth=auth)

    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2
    assert changed.json()["assurance_tier"] == "general-high-assurance"
    assert unchanged.status_code == 200
    assert unchanged.json()["version"] == 2
    assert stale.status_code == 409
    assert current.json()["enabled"] is True
    assert current.json()["version"] == 2

    update_events = [
        event for event in audit.json() if event["action"] == "repository.updated"
    ]
    assert len(update_events) == 1
    assert update_events[0]["details"]["changes"]["auth_profile_ref"] == {
        "changed": True
    }
    assert "git-readonly-secondary" not in str(update_events[0]["details"])


def test_repository_assurance_profile_is_fail_closed(auth, mutation_headers):
    with TestClient(app) as client:
        missing_profile = _register(
            client,
            auth,
            mutation_headers,
            assurance_tier="regulated-critical",
        )
        forbidden_profile = _register(
            client,
            auth,
            mutation_headers,
            assurance_profile="aviation",
        )
        regulated = _register(
            client,
            auth,
            mutation_headers,
            name="regulated-repository",
            remote_url="https://git.company.com/critical/regulatory.git",
            assurance_tier="regulated-critical",
            assurance_profile="aviation",
        )
        incomplete_transition = client.patch(
            f"/api/repositories/{regulated.json()['id']}",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": 1, "assurance_tier": "general-standard"},
        )
        complete_transition = client.patch(
            f"/api/repositories/{regulated.json()['id']}",
            auth=auth,
            headers=mutation_headers,
            json={
                "expected_version": 1,
                "assurance_tier": "general-standard",
                "assurance_profile": None,
            },
        )

    assert missing_profile.status_code == 422
    assert forbidden_profile.status_code == 422
    assert regulated.status_code == 201
    assert incomplete_transition.status_code == 422
    assert complete_transition.status_code == 200
    assert complete_transition.json()["version"] == 2
    assert complete_transition.json()["assurance_profile"] is None


def test_repository_list_filters_without_exposing_internal_identity(auth, mutation_headers):
    with TestClient(app) as client:
        github = _register(client, auth, mutation_headers)
        generic = _register(
            client,
            auth,
            mutation_headers,
            name="internal-tools",
            remote_url="https://git.company.com/platform/internal-tools.git",
            enabled=False,
        )
        enabled = client.get("/api/repositories?enabled=true", auth=auth)
        generic_only = client.get("/api/repositories?provider=generic", auth=auth)

    assert github.status_code == 201
    assert generic.status_code == 201
    assert [item["name"] for item in enabled.json()] == ["ai-orchestra"]
    assert [item["name"] for item in generic_only.json()] == ["internal-tools"]
    assert "remote_identity" not in enabled.json()[0]


def test_repository_mutations_require_manager_auth_and_control_header(auth, mutation_headers):
    with TestClient(app) as client:
        unauthenticated = client.post(
            "/api/repositories",
            headers=mutation_headers,
            json={
                "name": "repository",
                "remote_url": "https://github.com/owner/repository.git",
            },
        )
        missing_control_header = client.post(
            "/api/repositories",
            auth=auth,
            json={
                "name": "repository",
                "remote_url": "https://github.com/owner/repository.git",
            },
        )

    assert unauthenticated.status_code == 401
    assert missing_control_header.status_code == 400


def test_repository_table_constraints_reject_forged_operational_state():
    with SessionLocal() as db:
        db.add(
            Repository(
                name="forged",
                remote_url="https://github.com/owner/forged.git",
                remote_identity="github.com/owner/forged",
                remote_host="github.com",
                provider="github",
                enabled=True,
                status="trusted-without-validation",
                execution_profile="development",
                assurance_tier="general-standard",
                version=1,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(
            Repository(
                name="empty-assurance-profile",
                remote_url="https://github.com/owner/empty-profile.git",
                remote_identity="github.com/owner/empty-profile",
                remote_host="github.com",
                provider="github",
                enabled=True,
                status="pending_validation",
                execution_profile="development",
                assurance_tier="regulated-critical",
                assurance_profile="",
                version=1,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_repository_table_constraints_require_complete_ready_and_validating_evidence():
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(
            Repository(
                name="disabled-ready",
                remote_url="https://github.com/owner/disabled-ready.git",
                remote_identity="github.com/owner/disabled-ready",
                remote_host="github.com",
                provider="github",
                enabled=False,
                status="ready",
                default_branch="main",
                last_known_commit="a" * 40,
                last_fetched_at=now,
                sync_finished_at=now,
                sync_next_at=now + timedelta(hours=1),
                execution_profile="development",
                assurance_tier="general-standard",
                version=1,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(
            Repository(
                name="validating-without-start",
                remote_url="https://github.com/owner/validating-without-start.git",
                remote_identity="github.com/owner/validating-without-start",
                remote_host="github.com",
                provider="github",
                enabled=True,
                status="validating",
                sync_generation=1,
                sync_lease_owner="worker",
                sync_lease_expires_at=now + timedelta(minutes=5),
                execution_profile="development",
                assurance_tier="general-standard",
                version=1,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_repository_schema_has_auth_reference_but_no_credential_columns():
    columns = set(Repository.__table__.columns.keys())

    assert "auth_profile_ref" in columns
    assert columns.isdisjoint(
        {
            "access_token",
            "auth_token",
            "credential",
            "password",
            "private_key",
            "secret",
        }
    )


def test_repository_has_no_delete_endpoint(auth, mutation_headers):
    with TestClient(app) as client:
        repository_id = _register(client, auth, mutation_headers).json()["id"]
        deleted = client.delete(
            f"/api/repositories/{repository_id}",
            auth=auth,
            headers=mutation_headers,
        )

    assert deleted.status_code == 405


def test_repository_audit_rows_are_committed_atomically(auth, mutation_headers):
    with TestClient(app) as client:
        repository_id = _register(client, auth, mutation_headers).json()["id"]

    with SessionLocal() as db:
        repository = db.get(Repository, repository_id)
        event = db.query(AuditEvent).filter_by(
            action="repository.registered",
            entity_id=repository_id,
        ).one_or_none()

    assert repository is not None
    assert event is not None


def test_manager_can_request_versioned_validation_without_setting_operational_state(
    auth,
    mutation_headers,
):
    with TestClient(app) as client:
        created = _register(client, auth, mutation_headers).json()
        requested = client.post(
            f"/api/repositories/{created['id']}/validate",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": 1},
        )
        stale = client.post(
            f"/api/repositories/{created['id']}/validate",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": 1},
        )
        audit = client.get("/api/audit", auth=auth).json()

    assert requested.status_code == 200
    payload = requested.json()
    assert payload["status"] == "pending_validation"
    assert payload["version"] == 2
    assert payload["sync_requested_at"] is not None
    assert payload["sync_next_at"] is not None
    assert payload["default_branch"] is None
    assert payload["last_known_commit"] is None
    assert stale.status_code == 409
    event = next(
        item for item in audit if item["action"] == "repository.validation_requested"
    )
    assert event["details"] == {"version": 2}


def test_disabled_repository_cannot_be_queued_for_validation(auth, mutation_headers):
    with TestClient(app) as client:
        created = _register(
            client,
            auth,
            mutation_headers,
            name="disabled-repository",
            remote_url="https://github.com/example/disabled-repository.git",
            enabled=False,
        ).json()
        requested = client.post(
            f"/api/repositories/{created['id']}/validate",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": 1},
        )

    assert created["sync_next_at"] is None
    assert requested.status_code == 409


def test_auth_profile_change_revokes_ready_state_and_schedules_revalidation(
    auth,
    mutation_headers,
):
    with TestClient(app) as client:
        created = _register(client, auth, mutation_headers).json()

    with SessionLocal() as db:
        repository = db.get(Repository, created["id"])
        repository.status = "ready"
        repository.default_branch = "main"
        repository.last_known_commit = "a" * 40
        repository.last_fetched_at = repository.created_at
        repository.sync_finished_at = repository.created_at
        repository.sync_next_at = repository.created_at
        db.commit()

    with TestClient(app) as client:
        changed = client.patch(
            f"/api/repositories/{created['id']}",
            auth=auth,
            headers=mutation_headers,
            json={
                "expected_version": 1,
                "auth_profile_ref": "git-readonly-secondary",
            },
        )

    assert changed.status_code == 200, changed.text
    payload = changed.json()
    assert payload["status"] == "pending_validation"
    assert payload["sync_next_at"] is not None
    assert payload["version"] == 2
