from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from control_plane.app.main import app
from control_plane.app.settings import Settings


def test_health_does_not_require_auth():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_production_rejects_placeholder_passwords():
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="sqlite+pysqlite:///:memory:",
            server_password="CHANGE_ME_MANAGER_PASSWORD",
        )


def test_dashboard_requires_manager_auth(auth):
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        response = client.get("/", auth=auth)

    assert response.status_code == 200
    assert "Кабинет руководителя" in response.text


def test_state_change_requires_control_header(auth):
    with TestClient(app) as client:
        response = client.post(
            "/api/tasks",
            auth=auth,
            json={"title": "Проверить стратегию"},
        )

    assert response.status_code == 400


def test_task_lifecycle_is_audited(auth, mutation_headers):
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            auth=auth,
            headers=mutation_headers,
            json={
                "title": "Проверить качество рыночных данных",
                "domain": "trading",
                "risk_level": "high",
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        moved = client.patch(
            f"/api/tasks/{task_id}/status",
            auth=auth,
            headers=mutation_headers,
            json={"status": "in_progress"},
        )
        invalid = client.patch(
            f"/api/tasks/{task_id}/status",
            auth=auth,
            headers=mutation_headers,
            json={"status": "done"},
        )
        audit = client.get("/api/audit", auth=auth)

    assert moved.status_code == 200
    assert moved.json()["status"] == "in_progress"
    assert invalid.status_code == 409
    assert [event["action"] for event in audit.json()] == [
        "task.status_changed",
        "task.created",
    ]


def test_financial_approval_never_unlocks_orchestra_capability(auth, mutation_headers):
    with TestClient(app) as client:
        requested = client.post(
            "/api/approvals",
            auth=auth,
            headers=mutation_headers,
            json={
                "kind": "financial_execution",
                "requested_by": "execution-engineer",
                "reason": "Тест управленческого согласования",
            },
        )
        approval_id = requested.json()["id"]
        decided = client.post(
            f"/api/approvals/{approval_id}/decision",
            auth=auth,
            headers=mutation_headers,
            json={"decision": "approved", "comment": "Запись решения, не команда исполнения"},
        )
        guard = client.get("/api/capabilities/guard", auth=auth)

    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"
    assert guard.json()["production_deploy_allowed"] is False
    assert guard.json()["external_write_allowed"] is False
    assert guard.json()["financial_execution_allowed"] is False
    assert guard.json()["secret_access_allowed"] is False


def test_budget_update_and_usage_summary(auth, mutation_headers):
    with TestClient(app) as client:
        updated = client.put(
            "/api/budgets/high-risk-research",
            auth=auth,
            headers=mutation_headers,
            json={
                "monthly_limit": "4500.00",
                "warning_pct": 75,
                "hard_stop": True,
                "enabled": True,
            },
        )
        usage = client.post(
            "/api/usage",
            auth=auth,
            headers=mutation_headers,
            json={
                "role": "quant-researcher",
                "provider": "model-router",
                "model": "orchestra-quant",
                "input_tokens": 1000,
                "output_tokens": 250,
                "cost": "17.125000",
            },
        )
        summary = client.get("/api/summary", auth=auth)

    assert updated.status_code == 200
    assert updated.json()["monthly_limit"] == "4500.00"
    assert usage.status_code == 201
    assert summary.json()["month_cost"] == "17.125000"
