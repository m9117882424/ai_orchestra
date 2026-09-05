from fastapi.testclient import TestClient

from control_plane.app.main import app as core_app, get_opencode_client
from control_plane.app.production import app as production_app


class FakeOpenCode:
    def __init__(self):
        self.status = "busy"
        self.status_calls = 0

    def session_statuses(self):
        self.status_calls += 1
        return {"production-observer-session": {"type": self.status}}

    def messages(self, session_id):
        return [
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "would complete via legacy refresh"}],
            }
        ]

    def abort(self, session_id):
        return None


def test_production_refresh_is_observer_only(auth, mutation_headers):
    fake = FakeOpenCode()
    core_app.dependency_overrides[get_opencode_client] = lambda: fake
    try:
        with TestClient(production_app) as client:
            created = client.post(
                "/api/tasks",
                auth=auth,
                headers=mutation_headers,
                json={"title": "Observer-only lifecycle", "domain": "development"},
            )
            task_id = created.json()["id"]
            started = client.post(
                f"/api/tasks/{task_id}/execute",
                auth=auth,
                headers=mutation_headers,
            )
            execution_id = started.json()["id"]
            fake.status = "idle"

            refreshed = client.post(
                f"/api/executions/{execution_id}/refresh",
                auth=auth,
                headers=mutation_headers,
            )
            executions = client.get("/api/executions", auth=auth).json()
            tasks = client.get("/api/tasks", auth=auth).json()

        assert refreshed.status_code == 200
        assert isinstance(refreshed.json(), list)
        run = next(item for item in executions if item["id"] == execution_id)
        task = next(item for item in tasks if item["id"] == task_id)
        assert run["status"] == "queued"
        assert run["stage"] == "dispatch_pending"
        assert run["result"] == ""
        assert task["status"] == "in_progress"
        assert fake.status_calls == 0
    finally:
        core_app.dependency_overrides.pop(get_opencode_client, None)
