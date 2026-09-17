from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from control_plane.app.db import SessionLocal, engine
from control_plane.app.main import app
from control_plane.app.models import ExecutionRun, Repository, RunnerJob, Task, TaskWorkspace
from control_plane.app.opencode_client import OpenCodeError
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


class FakeOpenCode:
    def __init__(self):
        self.status = "busy"
        self.status_calls = 0
        self.message_calls = 0

    def session_statuses(self):
        self.status_calls += 1
        return {"session-test-1": {"type": self.status}}

    def messages(self, session_id):
        self.message_calls += 1
        assert session_id == "session-test-1"
        return [{
            "info": {"role": "assistant"},
            "parts": [{"type": "text", "text": "QA пройден. Результат готов."}],
        }]

    def abort(self, session_id):
        assert session_id == "session-test-1"
        return None


class FailingOpenCode(FakeOpenCode):
    def session_statuses(self):
        self.status_calls += 1
        raise OpenCodeError("simulated OpenCode outage")


def _seed_repository(
    *,
    status: str = "ready",
    commit: str = "a" * 40,
    branch: str = "main",
) -> str:
    now = datetime.now(timezone.utc)
    suffix = uuid4().hex
    ready = status == "ready"
    with SessionLocal() as db:
        repository = Repository(
            name=f"ready-{suffix}",
            remote_url=f"https://github.com/example/{suffix}.git",
            remote_identity=f"github.com/example/{suffix}",
            remote_host="github.com",
            provider="github",
            auth_profile_ref="git-readonly",
            default_branch=branch if ready else None,
            enabled=True,
            status=status,
            last_known_commit=commit if ready else None,
            last_fetched_at=now if ready else None,
            sync_failure_count=0,
            sync_finished_at=now if ready else None,
            sync_next_at=now,
        )
        db.add(repository)
        db.commit()
        return repository.id


def _seed_ready_repository() -> str:
    return _seed_repository()


def _create_development_task(client, auth, mutation_headers, title="Сделать тестовый модуль"):
    repository_id = _seed_ready_repository()
    created = client.post(
        "/api/tasks",
        auth=auth,
        headers=mutation_headers,
        json={
            "title": title,
            "domain": "development",
            "repository_id": repository_id,
        },
    )
    assert created.status_code == 201
    return created.json()["id"]


def _seed_running_execution(task_id: str, session_id: str = "session-test-1") -> str:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None
        task.status = "in_progress"
        run = ExecutionRun(
            task_id=task_id,
            status="running",
            stage="department_lead",
            opencode_session_id=session_id,
            assigned_roles=["department-lead"],
        )
        db.add(run)
        db.commit()
        return run.id


def test_development_execution_is_durably_preparing_before_opencode(auth, mutation_headers):
    with TestClient(app) as client:
        task_id = _create_development_task(client, auth, mutation_headers)
        started = client.post(
            f"/api/tasks/{task_id}/execute",
            auth=auth,
            headers=mutation_headers,
        )
        tasks = client.get("/api/tasks", auth=auth).json()
        audit = client.get("/api/audit", auth=auth).json()

    assert started.status_code == 201
    assert started.json()["status"] == "preparing"
    assert started.json()["stage"] == "workspace_pending"
    assert started.json()["contract_version"] == 2
    assert started.json()["repository_id"] is not None
    assert started.json()["workspace_id"] is not None
    assert started.json()["base_commit"] == "a" * 40
    assert started.json()["workspace_path"].startswith("/workspace/worktrees/managed/")
    assert started.json()["opencode_session_id"] is None
    assert started.json()["lease_generation"] == 0
    assert started.json()["heartbeat_at"] is None
    assert started.json()["deadline_at"] > started.json()["created_at"]
    assert next(t for t in tasks if t["id"] == task_id)["status"] == "in_progress"
    actions = [event["action"] for event in audit]
    assert "workspace.requested" in actions
    assert "execution.preparing" in actions
    with SessionLocal() as db:
        workspace = db.get(TaskWorkspace, started.json()["workspace_id"])
        assert workspace is not None
        assert workspace.status == "pending"
        assert workspace.base_commit == started.json()["base_commit"]


def test_development_execution_respects_workspace_fk_order(auth, mutation_headers):
    # Production PostgreSQL enforces execution_runs.workspace_id immediately.
    # Keep SQLite lightweight for the suite, but enforce FKs in this regression.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()

    try:
        with TestClient(app) as client:
            task_id = _create_development_task(client, auth, mutation_headers)
            started = client.post(
                f"/api/tasks/{task_id}/execute",
                auth=auth,
                headers=mutation_headers,
            )

        assert started.status_code == 201
        workspace_id = started.json()["workspace_id"]
        with SessionLocal() as db:
            workspace = db.get(TaskWorkspace, workspace_id)
            run = db.get(ExecutionRun, started.json()["id"])
            assert workspace is not None
            assert run is not None
            assert run.workspace_id == workspace.id
    finally:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()


def test_second_execute_is_rejected_while_first_is_preparing(auth, mutation_headers):
    with TestClient(app) as client:
        task_id = _create_development_task(client, auth, mutation_headers)
        first = client.post(
            f"/api/tasks/{task_id}/execute",
            auth=auth,
            headers=mutation_headers,
        )
        second = client.post(
            f"/api/tasks/{task_id}/execute",
            auth=auth,
            headers=mutation_headers,
        )

    assert first.status_code == 201
    assert second.status_code == 409
    assert "активный запуск" in second.json()["detail"]


def test_execution_requires_an_assigned_ready_repository(auth, mutation_headers):
    with TestClient(app) as client:
        without_repository = client.post(
            "/api/tasks",
            auth=auth,
            headers=mutation_headers,
            json={"title": "Нет репозитория", "domain": "development"},
        )
        no_repository_run = client.post(
            f"/api/tasks/{without_repository.json()['id']}/execute",
            auth=auth,
            headers=mutation_headers,
        )

        pending_repository_id = _seed_repository(status="pending_validation")
        pending_task = client.post(
            "/api/tasks",
            auth=auth,
            headers=mutation_headers,
            json={
                "title": "Репозиторий ещё не готов",
                "domain": "development",
                "repository_id": pending_repository_id,
            },
        )
        pending_run = client.post(
            f"/api/tasks/{pending_task.json()['id']}/execute",
            auth=auth,
            headers=mutation_headers,
        )

    assert no_repository_run.status_code == 409
    assert "назначьте репозиторий" in no_repository_run.json()["detail"]
    assert pending_run.status_code == 409
    assert "не готов" in pending_run.json()["detail"]
    with SessionLocal() as db:
        assert db.query(ExecutionRun).count() == 0
        assert db.query(TaskWorkspace).count() == 0


def test_execution_rejects_malformed_durable_repository_identity(
    auth,
    mutation_headers,
):
    repository_id = _seed_repository(commit="z" * 40)
    with TestClient(app) as client:
        task = client.post(
            "/api/tasks",
            auth=auth,
            headers=mutation_headers,
            json={
                "title": "Повреждённая repository identity",
                "domain": "development",
                "repository_id": repository_id,
            },
        )
        response = client.post(
            f"/api/tasks/{task.json()['id']}/execute",
            auth=auth,
            headers=mutation_headers,
        )

    assert response.status_code == 409
    assert "immutable identity" in response.json()["detail"]
    with SessionLocal() as db:
        assert db.query(ExecutionRun).count() == 0
        assert db.query(TaskWorkspace).count() == 0


def test_repository_assignment_is_frozen_during_active_execution(
    auth,
    mutation_headers,
):
    replacement_repository_id = _seed_ready_repository()
    with TestClient(app) as client:
        task_id = _create_development_task(client, auth, mutation_headers)
        started = client.post(
            f"/api/tasks/{task_id}/execute",
            auth=auth,
            headers=mutation_headers,
        )
        changed = client.patch(
            f"/api/tasks/{task_id}/repository",
            auth=auth,
            headers=mutation_headers,
            json={"repository_id": replacement_repository_id},
        )

    assert started.status_code == 201
    assert changed.status_code == 409
    assert "активного запуска" in changed.json()["detail"]


def test_abort_persists_idempotent_cancel_intent_without_opencode_call(auth, mutation_headers):
    from control_plane.app.main import get_opencode_client

    fake = FakeOpenCode()
    app.dependency_overrides[get_opencode_client] = lambda: fake
    try:
        with TestClient(app) as client:
            task_id = _create_development_task(client, auth, mutation_headers)
            started = client.post(
                f"/api/tasks/{task_id}/execute",
                auth=auth,
                headers=mutation_headers,
            ).json()
            first = client.post(
                f"/api/executions/{started['id']}/abort",
                auth=auth,
                headers=mutation_headers,
            )
            second = client.post(
                f"/api/executions/{started['id']}/abort",
                auth=auth,
                headers=mutation_headers,
            )
            audit = client.get("/api/audit", auth=auth).json()

        assert first.status_code == 200
        assert first.json()["status"] == "cancelled"
        assert first.json()["stage"] == "stopped"
        assert first.json()["cancel_requested_at"] is not None
        assert first.json()["lease_generation"] == 1
        assert second.json()["cancel_requested_at"] == first.json()["cancel_requested_at"]
        assert second.json()["lease_generation"] == first.json()["lease_generation"]
        assert sum(event["action"] == "execution.cancelled" for event in audit) == 1
        assert sum(event["action"] == "workspace.cleanup_requested" for event in audit) == 1
        assert fake.status_calls == 0
        assert fake.message_calls == 0
    finally:
        app.dependency_overrides.pop(get_opencode_client, None)


def test_preparing_progress_does_not_call_opencode(auth, mutation_headers):
    from control_plane.app.main import get_opencode_client

    fake = FakeOpenCode()
    app.dependency_overrides[get_opencode_client] = lambda: fake
    try:
        with TestClient(app) as client:
            task_id = _create_development_task(client, auth, mutation_headers, "Показать queued прогресс")
            started = client.post(
                f"/api/tasks/{task_id}/execute",
                auth=auth,
                headers=mutation_headers,
            )
            progress = client.get(
                f"/api/executions/{started.json()['id']}/progress",
                auth=auth,
            )

        assert progress.status_code == 200
        assert progress.json()["session_state"] == "preparing"
        assert progress.json()["items"] == []
        assert fake.status_calls == 0
        assert fake.message_calls == 0
    finally:
        app.dependency_overrides.pop(get_opencode_client, None)


def _seed_retained_workspace(*, has_changes: bool) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    repository_id = _seed_ready_repository()
    task_id = str(uuid4())
    workspace_id = str(uuid4())
    run_id = str(uuid4())
    base_commit = "a" * 40
    initial_tree = "b" * 40
    preflight_digest = "c" * 64
    with SessionLocal() as db:
        db.add(
            Task(
                id=task_id,
                title="Проверить очистку workspace",
                repository_id=repository_id,
                status="qa",
            )
        )
        db.add(
            TaskWorkspace(
                id=workspace_id,
                task_id=task_id,
                repository_id=repository_id,
                status="retained",
                base_commit=base_commit,
                base_branch="main",
                branch_name=(
                    f"ai-orchestra/task-{task_id.replace('-', '')[:12]}"
                    f"/run-{run_id.replace('-', '')}"
                ),
                opencode_path=f"/workspace/worktrees/managed/{workspace_id}",
                initial_tree=initial_tree,
                preflight_digest=preflight_digest,
                tracked_entries=2,
                current_head_commit=base_commit,
                current_tree=("d" * 40 if has_changes else initial_tree),
                change_digest="e" * 64,
                has_changes=has_changes,
                changed_file_count=1 if has_changes else 0,
                prepared_at=now,
                inspection_requested_at=now,
                inspected_at=now,
                version=1,
            )
        )
        db.add(
            ExecutionRun(
                id=run_id,
                task_id=task_id,
                contract_version=2,
                repository_id=repository_id,
                workspace_id=workspace_id,
                base_commit=base_commit,
                workspace_path=f"/workspace/worktrees/managed/{workspace_id}",
                workspace_tree=initial_tree,
                workspace_preflight_digest=preflight_digest,
                workspace_preflight_completed_at=now,
                workspace_runtime_preflight_digest=preflight_digest,
                workspace_runtime_verified_at=now,
                status="completed",
                stage="manager_review",
                finished_at=now,
            )
        )
        db.commit()
    return workspace_id, 1



def _seed_verified_runner_execution() -> tuple[str, str, str]:
    now = datetime.now(timezone.utc)
    repository_id = _seed_ready_repository()
    task_id = str(uuid4())
    workspace_id = str(uuid4())
    run_id = str(uuid4())
    base_commit = "a" * 40
    tree = "b" * 40
    digest = "c" * 64
    with SessionLocal() as db:
        db.add(
            Task(
                id=task_id,
                title="Runner verified execution",
                repository_id=repository_id,
                status="in_progress",
            )
        )
        db.add(
            TaskWorkspace(
                id=workspace_id,
                task_id=task_id,
                repository_id=repository_id,
                status="ready",
                base_commit=base_commit,
                base_branch="main",
                branch_name=(
                    f"ai-orchestra/task-{task_id.replace('-', '')[:12]}"
                    f"/run-{run_id.replace('-', '')}"
                ),
                opencode_path=f"/workspace/worktrees/managed/{workspace_id}",
                initial_tree=tree,
                preflight_digest=digest,
                tracked_entries=2,
                prepared_at=now,
                version=1,
            )
        )
        db.add(
            ExecutionRun(
                id=run_id,
                task_id=task_id,
                contract_version=2,
                repository_id=repository_id,
                workspace_id=workspace_id,
                base_commit=base_commit,
                workspace_path=f"/workspace/worktrees/managed/{workspace_id}",
                workspace_tree=tree,
                workspace_preflight_digest=digest,
                workspace_preflight_completed_at=now,
                workspace_runtime_preflight_digest=digest,
                workspace_runtime_verified_at=now,
                status="running",
                stage="department_lead",
                opencode_session_id=f"session-{run_id}",
            )
        )
        db.commit()
    return run_id, workspace_id, repository_id


def test_runner_job_enqueue_copies_verified_binding_and_is_idempotent(
    auth, mutation_headers
):
    run_id, workspace_id, repository_id = _seed_verified_runner_execution()
    key = str(uuid4())
    payload = {
        "idempotency_key": key,
        "argv": ["python3", "-c", "print('ok')"],
        "timeout_seconds": 90,
    }
    with TestClient(app) as client:
        first = client.post(
            f"/api/executions/{run_id}/runner-jobs",
            auth=auth,
            headers=mutation_headers,
            json=payload,
        )
        repeated = client.post(
            f"/api/executions/{run_id}/runner-jobs",
            auth=auth,
            headers=mutation_headers,
            json=payload,
        )
        listed = client.get(f"/api/executions/{run_id}/runner-jobs", auth=auth)
        audit = client.get("/api/audit", auth=auth).json()

    assert first.status_code == 201
    assert repeated.status_code == 201
    assert first.json()["id"] == repeated.json()["id"]
    assert first.json()["repository_id"] == repository_id
    assert first.json()["workspace_id"] == workspace_id
    assert first.json()["base_commit"] == "a" * 40
    assert first.json()["preflight_digest"] == "c" * 64
    assert first.json()["status"] == "queued"
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [first.json()["id"]]
    assert sum(event["action"] == "runner_job.queued" for event in audit) == 1


def test_runner_job_idempotency_key_rejects_payload_change(auth, mutation_headers):
    run_id, _, _ = _seed_verified_runner_execution()
    key = str(uuid4())
    with TestClient(app) as client:
        first = client.post(
            f"/api/executions/{run_id}/runner-jobs",
            auth=auth,
            headers=mutation_headers,
            json={"idempotency_key": key, "argv": ["make", "test"], "timeout_seconds": 60},
        )
        changed = client.post(
            f"/api/executions/{run_id}/runner-jobs",
            auth=auth,
            headers=mutation_headers,
            json={"idempotency_key": key, "argv": ["make", "lint"], "timeout_seconds": 60},
        )

    assert first.status_code == 201
    assert changed.status_code == 409
    with SessionLocal() as db:
        assert db.query(RunnerJob).count() == 1


def test_runner_job_rejects_client_supplied_binding_fields(auth, mutation_headers):
    run_id, workspace_id, _ = _seed_verified_runner_execution()
    with TestClient(app) as client:
        response = client.post(
            f"/api/executions/{run_id}/runner-jobs",
            auth=auth,
            headers=mutation_headers,
            json={
                "idempotency_key": str(uuid4()),
                "argv": ["true"],
                "timeout_seconds": 30,
                "workspace_id": workspace_id,
                "base_commit": "f" * 40,
                "preflight_digest": "f" * 64,
            },
        )

    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(RunnerJob).count() == 0


def test_runner_job_requires_current_repository_trust(auth, mutation_headers):
    run_id, _, repository_id = _seed_verified_runner_execution()
    with SessionLocal() as db:
        repository = db.get(Repository, repository_id)
        assert repository is not None
        repository.enabled = False
        repository.status = "unavailable"
        repository.last_known_commit = None
        repository.last_fetched_at = None
        repository.sync_finished_at = None
        db.commit()
    with TestClient(app) as client:
        response = client.post(
            f"/api/executions/{run_id}/runner-jobs",
            auth=auth,
            headers=mutation_headers,
            json={
                "idempotency_key": str(uuid4()),
                "argv": ["true"],
                "timeout_seconds": 30,
            },
        )

    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.query(RunnerJob).count() == 0

def test_cleanup_request_requires_clean_inspection_and_exact_version(
    auth,
    mutation_headers,
):
    workspace_id, version = _seed_retained_workspace(has_changes=False)
    with TestClient(app) as client:
        listed = client.get("/api/workspaces", auth=auth)
        accepted = client.post(
            f"/api/workspaces/{workspace_id}/cleanup",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": version},
        )
        stale = client.post(
            f"/api/workspaces/{workspace_id}/cleanup",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": version},
        )

    assert listed.status_code == 200
    listed_workspace = next(
        item for item in listed.json() if item["id"] == workspace_id
    )
    assert listed_workspace["generation"] == 0
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "cleanup_pending"
    assert accepted.json()["version"] == version + 1
    assert stale.status_code == 409
    assert "expected_version" in stale.json()["detail"]


def test_cleanup_request_never_deletes_changed_workspace(auth, mutation_headers):
    workspace_id, version = _seed_retained_workspace(has_changes=True)
    with TestClient(app) as client:
        rejected = client.post(
            f"/api/workspaces/{workspace_id}/cleanup",
            auth=auth,
            headers=mutation_headers,
            json={"expected_version": version},
        )

    assert rejected.status_code == 409
    assert "изменён" in rejected.json()["detail"]
    with SessionLocal() as db:
        workspace = db.get(TaskWorkspace, workspace_id)
        assert workspace is not None
        assert workspace.status == "retained"
        assert workspace.version == version


def test_core_api_has_no_browser_owned_refresh_lifecycle(auth, mutation_headers):
    from control_plane.app.main import get_opencode_client

    fake = FakeOpenCode()
    app.dependency_overrides[get_opencode_client] = lambda: fake
    try:
        with TestClient(app) as client:
            task_id = _create_development_task(client, auth, mutation_headers)
            run_id = _seed_running_execution(task_id)
            refreshed = client.post(
                f"/api/executions/{run_id}/refresh",
                auth=auth,
                headers=mutation_headers,
            )
            executions = client.get("/api/executions", auth=auth).json()
            tasks = client.get("/api/tasks", auth=auth).json()

        assert refreshed.status_code == 404
        assert next(r for r in executions if r["id"] == run_id)["status"] == "running"
        assert next(t for t in tasks if t["id"] == task_id)["status"] == "in_progress"
        assert fake.status_calls == 0
    finally:
        app.dependency_overrides.pop(get_opencode_client, None)


def test_execution_progress_exposes_live_messages_after_dispatch(auth, mutation_headers):
    from control_plane.app.main import get_opencode_client

    fake = FakeOpenCode()
    app.dependency_overrides[get_opencode_client] = lambda: fake
    try:
        with TestClient(app) as client:
            task_id = _create_development_task(client, auth, mutation_headers, "Показать живой прогресс")
            run_id = _seed_running_execution(task_id)
            progress = client.get(
                f"/api/executions/{run_id}/progress",
                auth=auth,
            )

        assert progress.status_code == 200
        payload = progress.json()
        assert payload["session_state"] == "busy"
        assert payload["elapsed_seconds"] >= 0
        assert payload["items"][-1]["text"] == "QA пройден. Результат готов."
    finally:
        app.dependency_overrides.pop(get_opencode_client, None)


def test_execution_progress_is_read_only_when_opencode_is_unavailable(auth, mutation_headers):
    from control_plane.app.evidence import record_evidence
    from control_plane.app.main import get_opencode_client

    fake = FailingOpenCode()
    app.dependency_overrides[get_opencode_client] = lambda: fake
    try:
        with TestClient(app) as client:
            task_id = _create_development_task(client, auth, mutation_headers)
            run_id = _seed_running_execution(task_id)
            with SessionLocal() as db:
                record_evidence(
                    db,
                    execution_id=run_id,
                    source="opencode",
                    source_key="message:durable-progress",
                    kind="message",
                    role="qa",
                    model="orchestra-qa",
                    status="observed",
                    details={"text": "Durable QA evidence survives OpenCode outage."},
                )
                db.commit()
            progress = client.get(
                f"/api/executions/{run_id}/progress",
                auth=auth,
            )
            executions = client.get("/api/executions", auth=auth).json()

        assert progress.status_code == 200
        assert progress.json()["session_state"] == "unavailable"
        assert progress.json()["error"] == "simulated OpenCode outage"
        assert progress.json()["current_role"] == "qa"
        assert progress.json()["items"][-1]["text"] == "Durable QA evidence survives OpenCode outage."
        assert next(r for r in executions if r["id"] == run_id)["status"] == "running"
        assert fake.status_calls == 1
        assert fake.message_calls == 0
    finally:
        app.dependency_overrides.pop(get_opencode_client, None)


def test_terminal_progress_without_session_does_not_call_opencode(auth, mutation_headers):
    from control_plane.app.main import get_opencode_client

    fake = FakeOpenCode()
    app.dependency_overrides[get_opencode_client] = lambda: fake
    try:
        with TestClient(app) as client:
            task_id = _create_development_task(client, auth, mutation_headers)
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                assert task is not None
                task.status = "failed"
                run = ExecutionRun(
                    task_id=task_id,
                    status="cancelled",
                    stage="stopped",
                    opencode_session_id=None,
                    assigned_roles=["department-lead"],
                )
                db.add(run)
                db.commit()
                run_id = run.id

            progress = client.get(
                f"/api/executions/{run_id}/progress",
                auth=auth,
            )

        assert progress.status_code == 200
        assert progress.json()["status"] == "cancelled"
        assert progress.json()["session_state"] == "cancelled"
        assert fake.status_calls == 0
        assert fake.message_calls == 0
    finally:
        app.dependency_overrides.pop(get_opencode_client, None)


def test_runner_job_api_uses_same_argv_limit_as_checkpoint_and_runnerd(auth, mutation_headers):
    run_id, _, _ = _seed_verified_runner_execution()
    with TestClient(app) as client:
        accepted = client.post(
            f"/api/executions/{run_id}/runner-jobs",
            auth=auth,
            headers=mutation_headers,
            json={
                "idempotency_key": str(uuid4()),
                "argv": ["x"] * 64,
                "timeout_seconds": 30,
            },
        )
        rejected = client.post(
            f"/api/executions/{run_id}/runner-jobs",
            auth=auth,
            headers=mutation_headers,
            json={
                "idempotency_key": str(uuid4()),
                "argv": ["x"] * 65,
                "timeout_seconds": 30,
            },
        )

    assert accepted.status_code == 201
    assert rejected.status_code == 422
