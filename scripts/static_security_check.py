#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_OPENCODE_ENV = {
    "AITUNNEL_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "CONTROL_PLANE_DB_PASSWORD",
    "CONTROL_PLANE_SERVER_PASSWORD",
    "MODEL_ROUTER_MASTER_KEY",
}
PRODUCT_POLICY_MARKERS = {
    "min_deviation_pct",
    "cash_reserve_min_pct",
    "cash_reserve_max_pct",
    "daily_purchase_limit",
}
ACTION_USE_RE = re.compile(r"uses:\s+[^@\s]+@([^\s#]+)")
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def resolved_compose() -> dict:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def network_set(service: dict) -> set[str]:
    value = service.get("networks") or {}
    return set(value.keys() if isinstance(value, dict) else value)


def assert_actions_pinned(path: Path, text: str) -> None:
    refs = ACTION_USE_RE.findall(text)
    for ref in refs:
        assert FULL_SHA_RE.fullmatch(ref), f"Mutable GitHub Action ref in {path}: @{ref}"


def main() -> int:
    cfg = resolved_compose()
    services = cfg["services"]

    opencode_env = set((services["opencode"].get("environment") or {}).keys())
    leaked = sorted(opencode_env & FORBIDDEN_OPENCODE_ENV)
    assert not leaked, f"OpenCode receives forbidden secrets: {leaked}"
    assert "MODEL_ROUTER_CLIENT_KEY" in opencode_env

    assert network_set(services["postgres"]) == {"control-db"}
    assert network_set(services["control-plane"]) == {"control-db", "control-access", "model-net"}
    assert network_set(services["model-router"]) == {"router-backend", "provider-egress"}
    assert network_set(services["model-gateway"]) == {"model-net", "router-backend"}
    assert network_set(services["opencode"]) == {"model-net"}
    control_env = set((services["control-plane"].get("environment") or {}).keys())
    assert "MODEL_ROUTER_CLIENT_KEY" not in control_env
    assert "MODEL_ROUTER_MASTER_KEY" not in control_env
    assert "CONTROL_PLANE_SCHEMA_MODE" not in control_env

    # OpenCode talks only to the inference gateway with a non-admin client credential.
    gateway = json.loads((ROOT / "config/opencode.gateway.json").read_text(encoding="utf-8"))
    assert set(gateway["provider"]) == {"orchestra"}
    options = gateway["provider"]["orchestra"]["options"]
    assert options["baseURL"] == "http://model-gateway:8080/v1"
    assert options["apiKey"] == "{env:MODEL_ROUTER_CLIENT_KEY}"

    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("AITUNNEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"):
        assert f"{key}=" not in env_text, f"{key} must live only in .env.providers"
    assert "MODEL_ROUTER_MASTER_KEY=" in env_text
    assert "MODEL_ROUTER_CLIENT_KEY=" in env_text
    assert "OPENCODE_VERSION=1.18.27" in env_text
    assert "LITELLM_VERSION=1.98.0" in env_text
    assert "CONTROL_PLANE_SCHEMA_MODE=" not in env_text

    provider_example = (ROOT / ".env.providers.example").read_text(encoding="utf-8")
    for key in ("AITUNNEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"):
        assert f"{key}=" in provider_example
    assert "MODEL_ROUTER_MASTER_KEY=" not in provider_example
    assert "MODEL_ROUTER_CLIENT_KEY=" not in provider_example

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    router_dockerfile = (ROOT / "model_router/Dockerfile").read_text(encoding="utf-8")
    control_dockerfile = (ROOT / "control_plane/Dockerfile").read_text(encoding="utf-8")
    assert "ARG OPENCODE_VERSION=1.18.27" in dockerfile
    assert "ARG LITELLM_VERSION=1.98.0" in router_dockerfile
    assert "=latest" not in dockerfile
    assert "ripgrep" in dockerfile
    assert "sha256sum --check dependency-locks.sha256" in control_dockerfile
    assert "--require-hashes --requirement requirements.lock" in control_dockerfile
    assert "runtime-lock.sha256" in control_dockerfile

    runtime_wrapper = (ROOT / "control_plane/requirements.txt").read_text(encoding="utf-8")
    dev_wrapper = (ROOT / "control_plane/requirements-dev.txt").read_text(encoding="utf-8")
    assert "--require-hashes" in runtime_wrapper and "-r requirements.lock" in runtime_wrapper
    assert "--require-hashes" in dev_wrapper and "-r requirements-dev.lock" in dev_wrapper

    lock_workflow_path = ROOT / ".github/workflows/generate-dependency-locks.yml"
    lock_workflow = lock_workflow_path.read_text(encoding="utf-8")
    assert "contents: read" in lock_workflow
    assert "contents: write" not in lock_workflow
    assert "git push" not in lock_workflow
    assert "pip-tools==7.6.1" in lock_workflow
    assert "--generate-hashes" in lock_workflow
    assert "git diff --exit-code -- requirements.lock requirements-dev.lock dependency-locks.sha256" in lock_workflow
    assert "pull_request:" in lock_workflow
    assert "feature/g1-reproducible-dependencies" not in lock_workflow
    assert_actions_pinned(lock_workflow_path, lock_workflow)

    validate_workflow_path = ROOT / ".github/workflows/validate.yml"
    validate_workflow = validate_workflow_path.read_text(encoding="utf-8")
    assert "runs-on: ubuntu-24.04" in validate_workflow
    assert "scripts/verify_dependency_locks.py" in validate_workflow
    assert "--require-hashes --requirement control_plane/requirements-dev.lock" in validate_workflow
    assert "Docker buildability with current upstream bases" in validate_workflow
    assert_actions_pinned(validate_workflow_path, validate_workflow)

    db_text = (ROOT / "control_plane/app/db.py").read_text(encoding="utf-8")
    schema_cli_text = (ROOT / "control_plane/app/schema_cli.py").read_text(encoding="utf-8")
    migrate_script = (ROOT / "scripts/migrate-control-plane.sh").read_text(encoding="utf-8")
    assert "CONTROL_PLANE_SCHEMA_MODE" not in db_text
    assert "CONTROL_PLANE_SCHEMA_MODE" not in schema_cli_text
    assert "pg_advisory_xact_lock" in schema_cli_text
    assert "pg_advisory_lock(" not in schema_cli_text
    assert "pg_advisory_unlock" not in schema_cli_text
    assert "from .db import engine" not in schema_cli_text
    assert "SKIP_PRE_MIGRATION_BACKUP" not in migrate_script
    assert "bash ./scripts/backup.sh" in migrate_script

    models_text = (ROOT / "control_plane/app/models.py").read_text(encoding="utf-8")
    for marker in PRODUCT_POLICY_MARKERS:
        assert marker not in models_text, f"Product policy leaked into Orchestra core: {marker}"

    shared = (ROOT / "config/model-router.shared.yaml").read_text(encoding="utf-8")
    direct = (ROOT / "config/model-router.separate.yaml").read_text(encoding="utf-8")
    for alias in ("orchestra-lead", "orchestra-architect", "orchestra-coder", "orchestra-analyst", "orchestra-qa"):
        assert f"model_name: {alias}" in shared
    for alias in ("orchestra-lead", "orchestra-architect", "orchestra-analyst"):
        assert f"model_name: {alias}" in direct
    assert "anthropic/claude-sonnet-5" in direct
    assert "openai/gpt-5.6-sol" in direct
    assert "gemini/gemini-3.5-flash" in direct
    assert "gemini/gemini-3.5-flash-lite" in direct

    print("[OK] static security boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
