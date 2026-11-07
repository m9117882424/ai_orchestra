import json
from ipaddress import ip_network
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from control_plane.app import execution_worker
from control_plane.app.assured_review import parse_assured_review_verdict
from control_plane.app.db import SessionLocal
from control_plane.app.execution_protocol import assured_review_prompt
from control_plane.app.execution_worker import (
    ExecutionLease, ExecutionLeaseManager, _require_assured_review_before_completion,
    poll_execution,
)
from control_plane.app.models import ExecutionEvidence, ExecutionRun, Task, UsageEvent
from control_plane.app.opencode_client import OpenCodeClient


def test_openai_only_review_profile_keeps_provider_credentials_outside_agent():
    root = Path(__file__).resolve().parents[2]
    router = yaml.safe_load(
        (root / "config/model-router.assured-openai.yaml").read_text(encoding="utf-8")
    )
    assert router["model_list"] == [{
        "model_name": "orchestra-reviewer",
        "litellm_params": {
            "model": "openai/gpt-6.1-sol",
            "api_key": "os.environ/OPENAI_API_KEY",
            "max_completion_tokens": 3072,
        },
    }]
    assert "AITUNNEL" not in json.dumps(router)
    agent_config = json.loads(
        (root / "config/opencode.assured-review.json").read_text(encoding="utf-8")
    )
    assert "OPENAI_API_KEY" not in json.dumps(agent_config)
    overlay = yaml.safe_load(
        (root / "compose.assured-openai.yml").read_text(encoding="utf-8")
    )
    assert list(overlay["services"]) == ["model-router"]
    assert overlay["services"]["model-router"]["volumes"] == [
        "./config/model-router.assured-openai.yaml:/app/config.yaml:ro"
    ]
    review = yaml.safe_load(
        (root / "compose.assured-review.yml").read_text(encoding="utf-8")
    )["services"]["opencode-review"]
    assert review["cap_drop"] == ["ALL"]
    assert review["cap_add"] == ["DAC_READ_SEARCH"]
    assert review["read_only"] is True
    assert "task-workspaces:/workspace/worktrees/managed:ro" in review["volumes"]
    assert "/root/.config/opencode:size=4m,mode=0700" in review["tmpfs"]
    assert "./config/opencode.assured-review.json:/root/.config/opencode/opencode.json:ro" in review["volumes"]


def test_optional_staging_networks_are_separate_and_never_reuse_prod_networks():
    root = Path(__file__).resolve().parents[2]
    overlay = yaml.safe_load(
        (root / "compose.assured-staging-network.yml").read_text(encoding="utf-8")
    )
    assert set(overlay) == {"services", "networks"}
    assert set(overlay["services"]) == {
        "control-plane", "execution-worker", "assured-review-worker",
        "repo-manager", "workspace-manager", "workspace-volume-init",
        "model-router", "model-gateway", "opencode", "opencode-review",
    }
    assert all(
        spec["image"].startswith("ai-orchestra-assured-staging/")
        for spec in overlay["services"].values()
    )
    assert overlay["services"]["opencode-review"]["image"] == (
        overlay["services"]["opencode"]["image"]
    )
    networks = overlay["networks"]
    assert set(networks) == {
        "control-db", "control-access", "model-net", "router-backend",
        "provider-egress", "repository-egress",
    }
    assert all("external" not in settings and "name" not in settings
               for settings in networks.values())
    assert all(networks[part]["internal"] for part in (
        "control-db", "control-access", "model-net", "router-backend",
    ))
    ranges = [
        ip_network(settings["ipam"]["config"][0]["subnet"])
        for settings in networks.values()
    ]
    assert all(part.prefixlen == 27 and part.subnet_of(ip_network("172.23.0.0/24"))
               for part in ranges)
    assert all(not first.overlaps(second)
               for i, first in enumerate(ranges) for second in ranges[i + 1:])


@pytest.mark.parametrize("payload", [
    "", "pass", '<AI_ORCHESTRA_VERDICT>{"version":1,"verdict":"pass","findings_count":1}</AI_ORCHESTRA_VERDICT>',
    '<AI_ORCHESTRA_VERDICT>{"version":1,"verdict":"pass","findings_count":false}</AI_ORCHESTRA_VERDICT>',
    '<AI_ORCHESTRA_VERDICT>{"version":1,"verdict":"pass","findings_count":0,"extra":"unexpected"}</AI_ORCHESTRA_VERDICT>',
    ('<AI_ORCHESTRA_VERDICT>{"version":1,"verdict":"pass","findings_count":0}</AI_ORCHESTRA_VERDICT>') * 2,
])
def test_assured_review_verdict_rejects_invalid_contract(payload):
    assert parse_assured_review_verdict(payload) is None


def test_assured_review_prompt_is_explicitly_read_only():
    task = Task(title="Review efficiency PR", description="Find regressions", project="Arvento")
    prompt = assured_review_prompt(task, branch="feature/efficiency", commit="a" * 40)
    assert "feature/efficiency" in prompt
    assert "a" * 40 in prompt
    assert "Никаких правок" in prompt
    assert "AI_ORCHESTRA_VERDICT" in prompt
    assert "Runner Manager" not in prompt


def test_assured_reviewer_agent_is_sent_explicitly_in_opencode_request(monkeypatch):
    sent = []
    monkeypatch.setattr(OpenCodeClient, "_request", lambda self, method, path, payload:
                        sent.append((method, path, payload)))
    client = OpenCodeClient("http://review-internal:4096", "user", "password")
    client.prompt_async("ses-review", "review", message_id="msg-1", part_id="prt-1",
                        agent="assured-reviewer")
    assert sent[0][2]["agent"] == "assured-reviewer"


def _seed_leased_review(*, metered: bool) -> tuple[ExecutionLeaseManager, ExecutionLease, str]:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        task = Task(title="Isolated review", domain="development", status="in_progress")
        db.add(task)
        db.flush()
        run = ExecutionRun(
            task_id=task.id, contract_version=1, status="running",
            backend="opencode-review", assigned_roles=["assured-reviewer"],
            lead_role="assured-reviewer", lease_owner="review-test", lease_generation=1,
            lease_expires_at=now + timedelta(minutes=2), deadline_at=now + timedelta(hours=1),
        )
        db.add(run)
        db.flush()
        if metered:
            db.add(UsageEvent(
                execution_id=run.id, task_id=task.id,
                source="opencode-message", source_key="msg-assistant-1",
                role="assured-reviewer", provider="orchestra", model="orchestra-reviewer",
                input_tokens=67, output_tokens=31, cost=0,
            ))
        db.commit()
        run_id = run.id
    lease = ExecutionLease(
        execution_id=run_id, generation=1, status="running", opencode_session_id="ses-review",
        deadline_at=now + timedelta(hours=1), cancel_requested_at=None,
        workspace_path=None, workspace_binding=None, assured_review=True,
    )
    return ExecutionLeaseManager("review-test"), lease, run_id


def test_assured_review_fails_closed_without_automatic_message_usage(monkeypatch):
    manager, lease, run_id = _seed_leased_review(metered=False)
    monkeypatch.setattr(execution_worker, "_checkpoint_workspace_snapshot",
                        lambda *args: ("snapshot", False))
    outcome = _require_assured_review_before_completion(
        manager, lease,
        '<AI_ORCHESTRA_VERDICT>{"version":1,"verdict":"pass","findings_count":0}</AI_ORCHESTRA_VERDICT>',
    )
    assert outcome == "failed"
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "failed"
        assert run.stage == "assured_review_metering_missing"


def test_assured_review_records_verdict_only_with_provenance_and_clean_source(monkeypatch):
    manager, lease, run_id = _seed_leased_review(metered=True)
    monkeypatch.setattr(execution_worker, "_checkpoint_workspace_snapshot",
                        lambda *args: ("snapshot", False))
    outcome = _require_assured_review_before_completion(
        manager, lease,
        'One P1 security finding.\n<AI_ORCHESTRA_VERDICT>'
        '{"version":1,"verdict":"fail","findings_count":1}</AI_ORCHESTRA_VERDICT>',
    )
    assert outcome == "allow"
    with SessionLocal() as db:
        review = db.scalar(select(ExecutionEvidence).where(
            ExecutionEvidence.execution_id == run_id, ExecutionEvidence.kind == "review",
        ))
        assert review.status == "fail"
        assert review.details["findings_count"] == 1
        assert db.get(ExecutionRun, run_id).status == "running"


def test_assured_review_rejects_dirty_workspace_even_with_metering(monkeypatch):
    manager, lease, run_id = _seed_leased_review(metered=True)
    monkeypatch.setattr(execution_worker, "_checkpoint_workspace_snapshot",
                        lambda *args: ("snapshot", True))
    outcome = _require_assured_review_before_completion(
        manager, lease,
        '<AI_ORCHESTRA_VERDICT>{"version":1,"verdict":"pass","findings_count":0}</AI_ORCHESTRA_VERDICT>',
    )
    assert outcome == "failed"
    with SessionLocal() as db:
        assert db.get(ExecutionRun, run_id).stage == "assured_review_source_modified"


def test_review_poll_end_to_end_records_metering_and_verdict_before_completion(monkeypatch):
    manager, lease, run_id = _seed_leased_review(metered=False)
    monkeypatch.setattr(execution_worker, "_checkpoint_workspace_snapshot",
                        lambda *args: ("snapshot", False))
    result = ('Static review: no issue observed.\n<AI_ORCHESTRA_VERDICT>'
              '{"version":1,"verdict":"pass","findings_count":0}</AI_ORCHESTRA_VERDICT>')

    class FakeReviewOpenCode:
        def session_statuses(self):
            return {"ses-review": {"type": "idle"}}

        def messages(self, session_id):
            assert session_id == "ses-review"
            return [{"info": {
                "id": "msg-assured", "role": "assistant", "agent": "assured-reviewer",
                "modelID": "orchestra-reviewer", "providerID": "orchestra",
                "tokens": {"input": 200, "output": 88}, "cost": 0.005,
                "finish": "stop", "time": {"completed": 1789632060000},
            }, "parts": [{"type": "text", "text": result}]}]

    outcome = poll_execution(manager, FakeReviewOpenCode, lease)
    assert outcome == "completed"
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert run.status == "completed"
        reviews = list(db.scalars(select(ExecutionEvidence).where(
            ExecutionEvidence.execution_id == run_id, ExecutionEvidence.kind == "review")))
        usage = list(db.scalars(select(UsageEvent).where(UsageEvent.execution_id == run_id)))
    assert [item.status for item in reviews] == ["pass"]
    assert len(usage) == 1
    assert (usage[0].input_tokens, usage[0].output_tokens) == (200, 88)


def test_execution_workers_cannot_claim_each_others_backend():
    default_manager = ExecutionLeaseManager("normal", backend="opencode")
    review_manager = ExecutionLeaseManager("review", backend="opencode-review")
    assert default_manager.backend == "opencode"
    assert review_manager.backend == "opencode-review"
    with pytest.raises(ValueError):
        ExecutionLeaseManager("bad", backend="codex-subscription")


def test_normal_worker_skips_assured_review_queue_and_review_worker_rejects_v1():
    _, _, run_id = _seed_leased_review(metered=False)
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        run.status = "queued"
        run.lease_owner = None
        run.lease_expires_at = None
        db.commit()
    normal = ExecutionLeaseManager("normal", backend="opencode")
    isolated = ExecutionLeaseManager("review", backend="opencode-review")
    with SessionLocal() as db:
        assert normal.claim_available(db, limit=1) == []
    with SessionLocal() as db:
        assert isolated.claim_available(db, limit=1) == []
        assert db.get(ExecutionRun, run_id).stage == "workspace_binding_rejected"
