"""Offline structural check of the opt-in subscription Compose overlay."""
import json
import os
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
env = os.environ.copy()
env.update({
    "CODEX_CP_TOKEN_FILE": "/tmp/synthetic-cp-token",
    "CODEX_WORKER_TOKEN_DIR": "/tmp/synthetic-worker-token-dir",
    "CODEX_PREFLIGHT_WORKSPACE_ID": "00000000-0000-0000-0000-000000000000",
    "CODEX_SANDBOX_SECCOMP_PROFILE": str(root / "deploy/codex-worker/security/codex-bwrap-seccomp.json"),
})
result = subprocess.run(
    ["docker", "compose", "-f", "docker-compose.yml", "-f",
     "deploy/docker-compose.codex-subscription.yml", "--profile", "codex-subscription",
     "config", "--format", "json"],
    cwd=root, env=env, capture_output=True, text=True, check=True,
)
config = json.loads(result.stdout)
services = config["services"]
worker = services["codex-subscription-worker"]
cp = services["control-plane"]

assert "codex-broker-relay" not in services
assert set(worker["networks"]) == {"codex-control", "codex-egress"}
assert "codex-control" in cp["networks"]
assert config["networks"]["codex-control"]["internal"] is True
assert worker["user"] == "10001:10001"
assert worker["read_only"]
assert worker["pids_limit"] == 128 and int(worker["mem_limit"]) == 4294967296
assert "ALL" in worker["cap_drop"]
assert "no-new-privileges:true" in worker["security_opt"]
assert "apparmor=ai-orchestra-codex-worker" in worker["security_opt"]
assert any(option.startswith("seccomp=") for option in worker["security_opt"])
assert not any("unconfined" in option for option in worker["security_opt"])
assert worker["restart"] == "no"
assert worker["profiles"] == ["codex-subscription"]
assert set(worker["environment"]) == {"HOME", "CODEX_HOME", "CODEX_PREFLIGHT_WORKSPACE_ID"}
assert cp["environment"]["CONTROL_PLANE_CODEX_WORKER_TOKEN_FILE"] == "/run/secrets/codex-worker-token"
mounts = {mount["target"]: mount for mount in worker["volumes"]}
assert mounts["/workspace/worktrees/managed"]["read_only"] is True
assert mounts["/home/orchestra"]["type"] == "volume"
assert mounts["/private/codex-results"]["type"] == "volume"
assert mounts["/run/codex-subscription-private"]["type"] == "bind"
assert mounts["/run/codex-subscription-private"].get("read_only", False) is False
assert cp["volumes"][0]["target"] == "/run/secrets/codex-worker-token"
assert cp["volumes"][0]["read_only"] is True
assert not worker.get("ports")
assert not any("docker.sock" in str(mount) for mount in worker["volumes"])
print("synthetic Codex Compose boundaries: OK")
