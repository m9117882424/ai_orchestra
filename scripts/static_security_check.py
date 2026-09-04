#!/usr/bin/env python3
from __future__ import annotations

import json
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
}
PRODUCT_POLICY_MARKERS = {
    "min_deviation_pct",
    "cash_reserve_min_pct",
    "cash_reserve_max_pct",
    "daily_purchase_limit",
}


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


def main() -> int:
    cfg = resolved_compose()
    services = cfg["services"]

    opencode_env = set((services["opencode"].get("environment") or {}).keys())
    leaked = sorted(opencode_env & FORBIDDEN_OPENCODE_ENV)
    assert not leaked, f"OpenCode receives forbidden secrets: {leaked}"

    assert network_set(services["postgres"]) == {"control-db"}
    assert network_set(services["control-plane"]) == {"control-db"}
    assert network_set(services["model-router"]) == {"model-net"}
    assert network_set(services["opencode"]) == {"model-net"}

    gateway = json.loads((ROOT / "config/opencode.gateway.json").read_text(encoding="utf-8"))
    assert set(gateway["provider"]) == {"orchestra"}
    api_key = gateway["provider"]["orchestra"]["options"]["apiKey"]
    assert api_key == "{env:MODEL_ROUTER_MASTER_KEY}"

    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("AITUNNEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"):
        assert f"{key}=" not in env_text, f"{key} must live only in .env.providers"

    provider_example = (ROOT / ".env.providers.example").read_text(encoding="utf-8")
    for key in ("AITUNNEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"):
        assert f"{key}=" in provider_example

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

    print("[OK] static security boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
