#!/usr/bin/env python3
from __future__ import annotations

import json
import yaml
import os
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
    "REPO_MANAGER_AUTH_PROFILES_JSON",
}
FORBIDDEN_EXECUTION_WORKER_ENV = {
    "AITUNNEL_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "MODEL_ROUTER_MASTER_KEY",
    "MODEL_ROUTER_CLIENT_KEY",
    "REPO_MANAGER_AUTH_PROFILES_JSON",
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


def resolved_runner_overlay() -> dict:
    env = os.environ.copy()
    env.update(
        {
            "RUNNERD_SOCKET_GID": "9999",
            "RUNNERD_SOCKET_HOST_PATH": "/run/ai-orchestra/runnerd.sock",
        }
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "deploy/docker-compose.runner-manager.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
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


def assert_compose_runs_disable_tty(path: Path, text: str) -> None:
    """Keep scripted Compose runs safe when stdin is a pipe, heredoc or scheduler."""
    logical_text = text.replace("\\\n", " ")
    marker = "docker compose run"
    for line_number, line in enumerate(logical_text.splitlines(), start=1):
        if marker not in line:
            continue
        arguments = line.split(marker, 1)[1]
        assert re.search(r"(?:^|\s)(?:-T|--no-TTY)(?=\s|$)", arguments), (
            f"Non-interactive Compose run must disable TTY in {path}:{line_number}"
        )


def main() -> int:
    cfg = resolved_compose()
    services = cfg["services"]
    for service_name, service in services.items():
        for mount in service.get("volumes") or []:
            source = str(mount.get("source") or "")
            target = str(mount.get("target") or "")
            assert "/var/run/docker.sock" not in {source, target}, (
                f"Docker socket must never be mounted into Compose service {service_name}"
            )

    opencode_env = set((services["opencode"].get("environment") or {}).keys())
    leaked = sorted(opencode_env & FORBIDDEN_OPENCODE_ENV)
    assert not leaked, f"OpenCode receives forbidden secrets: {leaked}"
    assert "MODEL_ROUTER_CLIENT_KEY" in opencode_env

    assert network_set(services["postgres"]) == {"control-db"}
    assert network_set(services["control-plane"]) == {"control-db", "control-access", "model-net"}
    assert network_set(services["execution-worker"]) == {"control-db", "model-net"}
    assert network_set(services["repo-manager"]) == {"control-db", "repository-egress"}
    assert network_set(services["workspace-manager"]) == {"control-db"}
    assert network_set(services["model-router"]) == {"router-backend", "provider-egress"}
    assert network_set(services["model-gateway"]) == {"model-net", "router-backend"}
    assert network_set(services["opencode"]) == {"model-net"}
    assert (cfg.get("networks") or {}).get("model-net", {}).get("internal") is True
    assert not (network_set(services["repo-manager"]) & network_set(services["opencode"]))
    assert not (network_set(services["repo-manager"]) & network_set(services["model-router"]))
    assert not (network_set(services["workspace-manager"]) & network_set(services["opencode"]))
    assert not (network_set(services["workspace-manager"]) & network_set(services["model-router"]))

    control_env = set((services["control-plane"].get("environment") or {}).keys())
    assert "MODEL_ROUTER_CLIENT_KEY" not in control_env
    assert "MODEL_ROUTER_MASTER_KEY" not in control_env
    assert "CONTROL_PLANE_SCHEMA_MODE" not in control_env

    worker = services["execution-worker"]
    worker_environment = worker.get("environment") or {}
    worker_env = set(worker_environment.keys())
    worker_leaks = sorted(worker_env & FORBIDDEN_EXECUTION_WORKER_ENV)
    assert not worker_leaks, f"Execution worker receives forbidden model/provider keys: {worker_leaks}"
    assert "CONTROL_PLANE_DB_PASSWORD" in worker_env
    assert "CONTROL_PLANE_OPENCODE_PASSWORD" in worker_env
    assert worker_environment.get("CONTROL_PLANE_SERVER_PASSWORD") == "execution-worker-does-not-use-manager-auth"
    assert not worker.get("ports"), "Execution worker must not expose host ports"
    worker_mounts = worker.get("volumes") or []
    assert len(worker_mounts) == 1
    assert worker_mounts[0].get("target") == "/workspace/worktrees/managed"
    assert worker_mounts[0].get("read_only") is True
    assert worker.get("read_only") is True
    assert worker.get("command") == ["python", "-m", "app.execution_worker"]
    assert worker.get("image") != services["control-plane"].get("image")
    assert worker.get("healthcheck"), "Execution worker must expose process liveness"
    assert worker_environment.get("CONTROL_PLANE_EXECUTION_WORKER_TOOL_STALL_SECONDS") == "300"

    repo_manager = services["repo-manager"]
    repo_environment = repo_manager.get("environment") or {}
    repo_env = set(repo_environment)
    assert "REPO_MANAGER_AUTH_PROFILES_JSON" in repo_env
    assert "CONTROL_PLANE_DB_PASSWORD" in repo_env
    assert repo_environment.get("CONTROL_PLANE_SERVER_PASSWORD") == (
        "repo-manager-does-not-use-manager-auth"
    )
    assert repo_environment.get("CONTROL_PLANE_OPENCODE_PASSWORD") == (
        "repo-manager-does-not-use-opencode-auth"
    )
    for forbidden in (
        "AITUNNEL_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "MODEL_ROUTER_MASTER_KEY",
        "MODEL_ROUTER_CLIENT_KEY",
    ):
        assert forbidden not in repo_env, f"Repo Manager receives forbidden key: {forbidden}"
    for service_name, service in services.items():
        if service_name != "repo-manager":
            assert "REPO_MANAGER_AUTH_PROFILES_JSON" not in (
                service.get("environment") or {}
            ), f"Git auth profiles leaked to {service_name}"
    assert not repo_manager.get("ports"), "Repo Manager must not expose host ports"
    assert repo_manager.get("read_only") is True
    assert repo_manager.get("command") == ["python", "-m", "app.repository_manager"]
    assert repo_manager.get("image") != services["control-plane"].get("image")
    assert (repo_manager.get("build") or {}).get("target") == "repo-manager"
    assert (services["control-plane"].get("build") or {}).get("target") == "control-plane"
    assert (worker.get("build") or {}).get("target") == "execution-worker"
    assert repo_manager.get("healthcheck"), "Repo Manager must expose process liveness"
    assert repo_manager.get("pids_limit") == 64
    repo_mounts = repo_manager.get("volumes") or []
    assert len(repo_mounts) == 1
    assert repo_mounts[0].get("target") == "/var/lib/ai-orchestra/repositories"

    workspace_manager = services["workspace-manager"]
    assert str(workspace_manager.get("user")) == "0:0"
    workspace_environment = workspace_manager.get("environment") or {}
    workspace_env = set(workspace_environment)
    assert "CONTROL_PLANE_DB_PASSWORD" in workspace_env
    assert workspace_environment.get("CONTROL_PLANE_SERVER_PASSWORD") == (
        "workspace-manager-does-not-use-manager-auth"
    )
    assert workspace_environment.get("CONTROL_PLANE_OPENCODE_PASSWORD") == (
        "workspace-manager-does-not-use-opencode-auth"
    )
    for forbidden in (
        "AITUNNEL_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "MODEL_ROUTER_MASTER_KEY",
        "MODEL_ROUTER_CLIENT_KEY",
        "REPO_MANAGER_AUTH_PROFILES_JSON",
    ):
        assert forbidden not in workspace_env, (
            f"Workspace Manager receives forbidden key: {forbidden}"
        )
    assert not workspace_manager.get("ports")
    assert workspace_manager.get("read_only") is True
    assert set(workspace_manager.get("cap_drop") or []) == {"ALL"}
    assert set(workspace_manager.get("cap_add") or []) == {
        "DAC_OVERRIDE",
        "FOWNER",
        "SETGID",
        "SETUID",
    }
    assert workspace_manager.get("command") == ["python", "-m", "app.workspace_manager"]
    assert workspace_manager.get("image") != services["control-plane"].get("image")
    assert (workspace_manager.get("build") or {}).get("target") == "workspace-manager"
    assert workspace_manager.get("entrypoint") is None
    assert workspace_manager.get("healthcheck")
    workspace_mounts = workspace_manager.get("volumes") or []
    assert {
        (mount.get("target"), bool(mount.get("read_only")))
        for mount in workspace_mounts
    } == {
        ("/var/lib/ai-orchestra/repositories", True),
        ("/workspace/worktrees/managed", False),
    }
    opencode_mounts = services["opencode"].get("volumes") or []
    task_mount = next(
        mount
        for mount in opencode_mounts
        if mount.get("target") == "/workspace/worktrees/managed"
    )
    assert task_mount.get("read_only") is not True
    assert set(services["opencode"].get("cap_drop") or []) == {"ALL"}
    assert set(services["opencode"].get("cap_add") or []) == {
        "DAC_OVERRIDE",
        "FOWNER",
    }
    manual_mount = next(
        mount
        for mount in opencode_mounts
        if mount.get("target") == "/workspace/worktrees/manual"
    )
    assert manual_mount.get("type") == "bind"
    assert not any(
        mount.get("target") == "/workspace/worktrees"
        for mount in opencode_mounts
    ), "A parent worktrees mount would mask the managed workspace volume"

    volume_init = services["workspace-volume-init"]
    assert volume_init.get("network_mode") == "none"
    assert volume_init.get("user") == "0:0"
    assert volume_init.get("read_only") is True
    assert not volume_init.get("environment")
    assert not volume_init.get("ports")
    assert set(volume_init.get("cap_add") or []) == {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
    }
    assert set(volume_init.get("cap_drop") or []) == {"ALL"}
    init_entrypoint = volume_init.get("entrypoint") or []
    assert init_entrypoint == ["/app/app/workspace_volume_init.sh"]
    assert not volume_init.get("command")
    init_script = (ROOT / "control_plane/app/workspace_volume_init.sh").read_text(
        encoding="utf-8"
    )
    for required in (
        '[ "$(id -u)" = 0 ]',
        '[ ! -L "$target" ]',
        'chown 10001:10001 "$target"',
        'chmod 0700 "$target"',
        "stat -c '%u:%g:%a'",
        "10001:10001:700",
    ):
        assert required in init_script
    init_mounts = volume_init.get("volumes") or []
    assert len(init_mounts) == 1
    assert init_mounts[0].get("target") == "/workspace/worktrees/managed"
    assert init_mounts[0].get("read_only") is not True
    assert (
        (workspace_manager.get("depends_on") or {})["workspace-volume-init"]
        ["condition"]
        == "service_completed_successfully"
    )
    assert (
        (workspace_manager.get("depends_on") or {})["repo-manager"]["condition"]
        == "service_healthy"
    )
    assert (
        (services["opencode"].get("depends_on") or {})["workspace-volume-init"]
        ["condition"]
        == "service_completed_successfully"
    )

    # OpenCode talks only to the inference gateway with a non-admin client credential.
    gateway = json.loads((ROOT / "config/opencode.gateway.json").read_text(encoding="utf-8"))
    assert gateway["permission"]["external_directory"] == "deny"
    assert gateway["permission"]["todowrite"] == "allow"
    assert gateway["permission"]["question"] == "deny"
    assert gateway["permission"]["bash"] == "deny"
    for agent_name, agent in gateway.get("agent", {}).items():
        assert agent.get("permission", {}).get("bash") == "deny", f"{agent_name} must not execute project shell"
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
    assert "CONTROL_PLANE_EXECUTION_TIMEOUT_SECONDS=7200" in env_text
    assert "CONTROL_PLANE_EXECUTION_WORKER_TOOL_STALL_SECONDS=300" in env_text
    assert "REPO_MANAGER_LEASE_SECONDS=300" in env_text
    assert "REPO_MANAGER_GIT_TIMEOUT_SECONDS=60" in env_text
    assert "REPO_MANAGER_MAX_ACTIVE=1" in env_text
    assert "REPO_MANAGER_MIN_FREE_BYTES=536870912" in env_text
    assert "WORKSPACE_MANAGER_LEASE_SECONDS=180" in env_text
    assert "WORKSPACE_MANAGER_GIT_TIMEOUT_SECONDS=60" in env_text
    assert "WORKSPACE_MANAGER_MAX_ACTIVE=1" in env_text
    assert "WORKSPACE_MANAGER_MAX_FILES=100000" in env_text
    assert "BACKUP_OFFSITE_ENCRYPTION_AT_REST_CONFIRMED=no" in env_text
    assert "BACKUP_OFFSITE_AUTHENTICATED_TRANSPORT_CONFIRMED=no" in env_text

    provider_example = (ROOT / ".env.providers.example").read_text(encoding="utf-8")
    for key in ("AITUNNEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"):
        assert f"{key}=" in provider_example
    assert "MODEL_ROUTER_MASTER_KEY=" not in provider_example
    assert "MODEL_ROUTER_CLIENT_KEY=" not in provider_example

    repository_example = (ROOT / ".env.repositories.example").read_text(encoding="utf-8")
    assert "REPO_MANAGER_AUTH_PROFILES_JSON='{}'" in repository_example
    for forbidden in (
        "AITUNNEL_API_KEY=",
        "OPENAI_API_KEY=",
        "CONTROL_PLANE_DB_PASSWORD=",
        "MODEL_ROUTER_MASTER_KEY=",
    ):
        assert forbidden not in repository_example

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
    assert "chmod -R a+rX /app/app /app/migrations" in control_dockerfile
    assert "chmod 0444 /app/alembic.ini" in control_dockerfile
    assert "app.production:app" in control_dockerfile
    assert "FROM application-base AS control-plane" in control_dockerfile
    assert "FROM git-runtime AS repo-manager" in control_dockerfile
    assert "FROM git-runtime AS workspace-manager" in control_dockerfile
    assert 'ENTRYPOINT ["python", "/app/app/workspace_manager_capability_launcher.py"]' in control_dockerfile
    assert "USER root" in control_dockerfile
    launcher_text = (ROOT / "control_plane/app/workspace_manager_capability_launcher.py").read_text(
        encoding="utf-8"
    )
    assert "data[0] = _CapabilityData(mask, mask, mask)" in launcher_text
    assert "FROM git-runtime AS execution-worker" in control_dockerfile
    assert "groupadd --gid 10001 orchestra" in control_dockerfile
    assert "useradd --uid 10001 --gid orchestra" in control_dockerfile
    assert "apt-get install -y --no-install-recommends ca-certificates git" in control_dockerfile
    assert "repo_manager_askpass.py" in control_dockerfile

    production_wrapper = (ROOT / "control_plane/app/production.py").read_text(encoding="utf-8")
    assert "_EXECUTION_REFRESH_RE" in production_wrapper
    assert 'scope["method"] = "GET"' in production_wrapper
    assert 'scope["path"] = "/api/executions"' in production_wrapper

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
    assert "docker compose run --rm -T --no-deps workspace-volume-init" in validate_workflow
    assert "[CHECK] Task workspace volume initialization" in validate_workflow
    assert 'test "$(id -u)" = 0 &&' in validate_workflow
    assert "chmod 0640 /workspace/worktrees/managed/.ci-workspace-boundary" in validate_workflow
    assert "shellcheck control_plane/app/workspace_volume_init.sh" in validate_workflow
    assert_actions_pinned(validate_workflow_path, validate_workflow)

    compose_command_paths = [
        ROOT / "Makefile",
        validate_workflow_path,
        *sorted((ROOT / "scripts").glob("*.sh")),
    ]
    for path in compose_command_paths:
        assert_compose_runs_disable_tty(path, path.read_text(encoding="utf-8"))

    for path in (
        ROOT / "scripts/worktree-create.sh",
        ROOT / "scripts/worktree-remove.sh",
    ):
        assert 'container_target="/workspace/worktrees/manual/' in path.read_text(
            encoding="utf-8"
        )

    main_text = (ROOT / "control_plane/app/main.py").read_text(encoding="utf-8")
    assert "Base.metadata.create_all" not in main_text
    assert ".metadata.create_all(" not in main_text
    assert "opencode.create_session" not in main_text, "HTTP execute boundary must persist before OpenCode side effects"
    assert "opencode.prompt_async" not in main_text, "HTTP execute boundary must not dispatch prompts"
    assert 'status="preparing"' in main_text
    assert 'stage="workspace_pending"' in main_text
    assert "workspace_branch_name" in main_text
    assert "workspace_path_for" in main_text
    assert "/api/executions/{execution_id}/refresh" not in main_text
    assert "deadline_at=" in main_text
    assert 'action="execution.preparing"' in main_text
    assert "opencode.abort" not in main_text

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
    assert migrate_script.count("docker compose run --rm -T --no-deps") == 2

    backup_script = (ROOT / "scripts/backup.sh").read_text(encoding="utf-8")
    verify_backup_script = (ROOT / "scripts/verify-backup.sh").read_text(encoding="utf-8")
    restore_drill_script = (ROOT / "scripts/restore-drill.sh").read_text(encoding="utf-8")
    offsite_script = (ROOT / "scripts/export-backup-offsite.sh").read_text(encoding="utf-8")
    assert "export-backup-offsite.sh" not in backup_script, "Local backup must not gain implicit external effects"
    assert 'flock -n 9' in backup_script
    assert 'bash ./scripts/verify-backup.sh "$archive"' in backup_script
    assert "printf '3\\n' > \"$staging_dir/BACKUP_FORMAT\"" in backup_script
    assert "control-plane execution-worker workspace-manager opencode" in backup_script
    assert "docker compose pause" in backup_script
    assert "docker compose unpause" in backup_script
    assert 'task-workspaces.tar.gz' in backup_script
    assert "--entrypoint tar workspace-volume-init" in backup_script
    assert "--entrypoint tar workspace-manager" not in backup_script
    assert "--lock-wait-timeout=30s" in backup_script
    assert "timeout --foreground" in backup_script
    assert "sha256sum --check --strict --quiet SHA256SUMS" in verify_backup_script
    assert "Secret-bearing file detected inside backup" in verify_backup_script
    assert ".env.repositories" in verify_backup_script
    assert "forbidden outer archive entry type" in verify_backup_script
    assert "forbidden workspace entry type" in verify_backup_script
    assert "escaping workspace symlink" in verify_backup_script
    assert 'or ".git" in path.parts' in verify_backup_script
    assert "BACKUP_FORMAT" in verify_backup_script
    assert "configuration/runner/runnerd.py" in verify_backup_script
    assert "configuration/runner/systemd/ai-orchestra-runnerd.service" in verify_backup_script
    assert 'backup_root="${BACKUP_ROOT:-$project_root/backups}"' in backup_script
    assert 'evidence_dir="${BACKUP_ROOT:-$project_root/backups}/drills"' in restore_drill_script
    backup_restore_smoke = (ROOT / "scripts/backup-restore-smoke.sh").read_text(encoding="utf-8")
    postgres_schema_smoke = (ROOT / "scripts/postgres-schema-smoke.sh").read_text(encoding="utf-8")
    assert 'export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-orchestra-backup-restore-smoke-' in backup_restore_smoke
    assert 'if [[ ! "$COMPOSE_PROJECT_NAME" =~ ^ai-orchestra-backup-restore-smoke-' in backup_restore_smoke
    assert 'COMPOSE_PROJECT_NAME="ai-orchestra-postgres-schema-smoke-' in postgres_schema_smoke
    assert 'rm -rf "$project_root/backups"' not in backup_restore_smoke
    assert 'BACKUP_ROOT="${BACKUP_ROOT:-$(mktemp -d /tmp/ai-orchestra-backup-smoke.' in backup_restore_smoke
    assert 'if [[ "$BACKUP_ROOT" != /tmp/ai-orchestra-backup-smoke.* ]]' in backup_restore_smoke
    assert 'Backup/restore smoke BACKUP_ROOT must be an isolated /tmp path' in backup_restore_smoke
    for destructive_smoke in (backup_restore_smoke, postgres_schema_smoke):
        assert 'docker compose down -v --remove-orphans' in destructive_smoke
        assert 'COMPOSE_FILE=' in destructive_smoke
        assert 'ai-orchestra/' in destructive_smoke
        assert 'docker image rm -f' in destructive_smoke
    assert 'ai-development-department' in postgres_schema_smoke
    assert 'Refusing destructive schema smoke in production Compose namespace' in postgres_schema_smoke
    assert 'control-plane-schema-smoke:' in postgres_schema_smoke
    assert 'control-plane-backup-smoke:' in backup_restore_smoke
    assert 'workspace-manager-backup-smoke:' in backup_restore_smoke
    assert "ai-orchestra-restore-net-" in restore_drill_script
    assert "ai-orchestra-restore-vol-" in restore_drill_script
    assert "--network-alias restore-postgres" in restore_drill_script
    assert "CONTROL_PLANE_DATABASE_URL" in restore_drill_script
    assert "observed_restore_rto_seconds" in restore_drill_script
    assert "task_workspace_restore" in restore_drill_script
    assert "task-workspaces.tar.gz" in restore_drill_script
    assert "Restored workspace directories missing database rows" in restore_drill_script
    assert "verify_restored_git_workspace" in restore_drill_script
    assert "Runner job restore reconciled" in restore_drill_script
    assert "runner_job_restore" in restore_drill_script
    assert "docker compose exec" not in restore_drill_script, "Restore drill must never execute against production Compose services"
    assert "BACKUP_OFFSITE_ENCRYPTION_AT_REST_CONFIRMED" in offsite_script
    assert "BACKUP_OFFSITE_AUTHENTICATED_TRANSPORT_CONFIRMED" in offsite_script
    assert "Refusing to overwrite existing offsite backup" in offsite_script
    assert "sha256sum" in offsite_script

    models_text = (ROOT / "control_plane/app/models.py").read_text(encoding="utf-8")
    for marker in PRODUCT_POLICY_MARKERS:
        assert marker not in models_text, f"Product policy leaked into Orchestra core: {marker}"
    for marker in (
        "lease_owner",
        "lease_generation",
        "heartbeat_at",
        "lease_expires_at",
        "deadline_at",
        "cancel_requested_at",
    ):
        assert marker in models_text, f"Execution fencing state missing: {marker}"

    repository_model = models_text.split("class Repository(Base):", 1)[1].split(
        "\n\nclass Task(Base):", 1
    )[0]
    assert "auth_profile_ref" in repository_model
    for forbidden_column in (
        "access_token",
        "auth_token",
        "credential",
        "password",
        "private_key",
        "secret",
    ):
        assert not re.search(
            rf"^\s+{forbidden_column}:\s+Mapped",
            repository_model,
            re.MULTILINE,
        ), f"Repository Registry must not store Git credential column: {forbidden_column}"
    assert 'status="pending_validation"' in main_text
    assert '@app.delete("/api/repositories' not in main_text
    assert '"/api/repositories/{repository_id}/validate"' in main_text

    repository_policy = (ROOT / "control_plane/app/repository_policy.py").read_text(
        encoding="utf-8"
    )
    assert 'parsed.scheme.lower() != "https"' in repository_policy
    assert "parsed.username is not None or parsed.password is not None" in repository_policy
    assert "ip_address(host)" in repository_policy
    assert "SECRET_LIKE_PREFIXES" in repository_policy
    for network_marker in ("subprocess", "socket.", "requests.", "httpx."):
        assert network_marker not in repository_policy, (
            "Repository Registry policy must remain validation-only; "
            f"unexpected network/process marker: {network_marker}"
        )

    repo_manager_text = (ROOT / "control_plane/app/repository_manager.py").read_text(
        encoding="utf-8"
    )
    for marker in (
        "with_for_update(skip_locked=True)",
        "sync_generation",
        "record_version",
        "http.followRedirects=false",
        "http.curloptResolve",
        "start_new_session=True",
        "os.killpg",
        "GIT_ALLOW_PROTOCOL",
        "protocol.allow=never",
        "fetch.fsckObjects=true",
        "--prune",
        "ls-remote",
        "resolve_public_addresses",
        "REPO_MANAGER_AUTH_PROFILES_JSON",
        "repo_manager_git_wrapper.sh",
        "storage_capacity_low",
        "_cleanup_staging",
        "_cleanup_stale_git_locks",
        "_repository_lock",
        "reconcile_mirror_cache",
        "mirror_cache_missing",
    ):
        assert marker in repo_manager_text, f"Repo Manager safety marker missing: {marker}"
    assert "shell=True" not in repo_manager_text
    assert "stderr=subprocess.DEVNULL" in repo_manager_text
    askpass_text = (ROOT / "control_plane/app/repo_manager_askpass.py").read_text(
        encoding="utf-8"
    )
    assert "REPO_MANAGER_GIT_PASSWORD" in askpass_text
    assert "logging" not in askpass_text
    assert "REPO_MANAGER_GIT_HOST" in askpass_text
    git_wrapper_text = (ROOT / "control_plane/app/repo_manager_git_wrapper.sh").read_text(
        encoding="utf-8"
    )
    assert 'ulimit -f "$limit"' in git_wrapper_text
    assert 'exec /usr/bin/git "$@"' in git_wrapper_text

    worker_text = (ROOT / "control_plane/app/execution_worker.py").read_text(encoding="utf-8")
    assert "with_for_update(skip_locked=True)" in worker_text
    assert "run.lease_generation" in worker_text
    assert "expires_at <= now" in worker_text
    assert "Rejected stale execution observation" in worker_text
    assert 'ACTIVE_EXECUTION_STATUSES = ("queued", "running")' in worker_text
    assert "sessions_for_execution" in worker_text
    assert "execution_message_id" in worker_text
    assert "execution_part_id" in worker_text
    assert "_renew_before_external_side_effect" in worker_text
    assert "timeout_execution" in worker_text
    assert "mark_timeout_pending" in worker_text
    assert "cancel_execution" in worker_text
    assert "mark_cancel_pending" in worker_text
    assert "delete_session" in worker_text
    assert "write_worker_health" in worker_text
    assert "verify_runtime_workspace" in worker_text
    assert "workspace_runtime_preflight_digest" in worker_text
    assert "workspace_root=workspace_root" in worker_text
    assert "request_workspace_inspection" in worker_text
    assert "client.for_directory" in worker_text
    assert 'minimum=60' in worker_text, "Runtime execution lease must exceed the 30s OpenCode HTTP timeout"

    workspace_manager_text = (ROOT / "control_plane/app/workspace_manager.py").read_text(
        encoding="utf-8"
    )
    for marker in (
        "with_for_update(skip_locked=True)",
        "workspace.binding_rejected",
        "workspace_manifest_binding_mismatch",
        "--ignored=matching",
        "repository_submodule_forbidden",
        "WorkspaceLeaseLost",
        "write_worker_health",
        "assert_repository_ready",
        "repository_trust_revoked",
    ):
        assert marker in workspace_manager_text, (
            f"Workspace Manager safety marker missing: {marker}"
        )
    assert "shell=True" not in workspace_manager_text
    workspace_protocol_text = (
        ROOT / "control_plane/app/workspace_protocol.py"
    ).read_text(encoding="utf-8")
    for marker in (
        "workspace_symlink_escape",
        "workspace_symlink_git_metadata",
        "workspace_manifest_digest_invalid",
        "O_NOFOLLOW",
        "verify_runtime_workspace",
        "start_new_session=True",
        "os.killpg",
    ):
        assert marker in workspace_protocol_text, (
            f"Workspace protocol safety marker missing: {marker}"
        )
    assert "shell=True" not in workspace_protocol_text

    runnerd_text = (ROOT / "runner/runnerd.py").read_text(encoding="utf-8")
    runner_entrypoint = (ROOT / "runner/entrypoint.py").read_text(encoding="utf-8")
    runner_dockerfile = (ROOT / "runner/Dockerfile").read_text(encoding="utf-8")
    runner_service = (ROOT / "runner/systemd/ai-orchestra-runnerd.service").read_text(encoding="utf-8")
    runner_smoke = (ROOT / "scripts/runner-isolation-smoke.sh").read_text(encoding="utf-8")
    runner_manager_smoke = (ROOT / "scripts/runner-manager-durable-smoke.sh").read_text(encoding="utf-8")
    assert 'control-plane-runner-smoke:' in runner_manager_smoke
    assert 'runner-manager-control-smoke:' in runner_manager_smoke
    assert 'docker image rm -f "$TAG" "$CONTROL_TAG" "$MANAGER_TAG"' in runner_manager_smoke
    for marker in (
        '"--network", "none"', '"--read-only"', '"--cap-drop", "ALL"',
        'no-new-privileges:true', 'volume-subpath=', 'dst=/source', 'readonly',
        '/workspace:rw,nosuid,nodev,size=', '"--pull", "never"', 'cleanup_uncertain',
    ):
        assert marker in runnerd_text, f"Disposable runner safety marker missing: {marker}"
    assert "shell=True" not in runnerd_text
    assert "/var/run/docker.sock" not in runnerd_text
    for marker in ('SOURCE = Path("/source")', 'WORKSPACE = Path("/workspace")',
                   "verify_manifest", "shutil.copytree", "os.execvp"):
        assert marker in runner_entrypoint, f"Runner entrypoint safety marker missing: {marker}"
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", runner_dockerfile, re.MULTILINE)
    for marker in ("PrivateNetwork=true", "PrivateDevices=true", "ProtectSystem=strict",
                   "ProtectHome=true", "RestrictAddressFamilies=AF_UNIX",
                   "CapabilityBoundingSet=", "NoNewPrivileges=true"):
        assert marker in runner_service, f"runnerd systemd hardening missing: {marker}"
    assert "G3 runner isolation smoke passed" in runner_smoke
    assert "RUNNERD_ALLOW_NONROOT=1 python3 runner/runnerd.py serve" in runner_smoke
    assert "RUNNERD_ALLOW_NONROOT" not in runner_service
    runner_env_example = (ROOT / "runner/runnerd.env.example").read_text(encoding="utf-8")
    assert "RUNNERD_ALLOW_NONROOT" not in runner_env_example
    assert "{{.Mountpoint}}" not in runner_smoke
    assert "/var/lib/docker/volumes" not in runner_smoke
    assert '-v "$VOLUME:/v:ro"' in runner_smoke
    assert "docker ps -aq --filter 'name=^ai-orchestra-runner-'" not in runner_smoke


    runner_manager_text = (
        ROOT / "control_plane/app/runner_manager.py"
    ).read_text(encoding="utf-8")
    runner_overlay = resolved_runner_overlay()
    runner_manager_service = runner_overlay["services"]["runner-manager"]
    assert network_set(runner_manager_service) == {"control-db"}
    assert str(runner_manager_service.get("user")) == "10001:10001"
    assert runner_manager_service.get("read_only") is True
    assert set(runner_manager_service.get("cap_drop") or []) == {"ALL"}
    runner_mounts = runner_manager_service.get("volumes") or []
    assert len(runner_mounts) == 1
    assert runner_mounts[0].get("target") == "/run/ai-orchestra/runnerd.sock"
    assert str(runner_mounts[0].get("source")) == "/run/ai-orchestra/runnerd.sock"
    assert runner_mounts[0].get("read_only") is True
    runner_env = runner_manager_service.get("environment") or {}
    assert runner_env.get("CONTROL_PLANE_SERVER_PASSWORD") == (
        "runner-manager-does-not-use-manager-auth"
    )
    assert runner_env.get("CONTROL_PLANE_OPENCODE_PASSWORD") == (
        "runner-manager-does-not-use-opencode-auth"
    )
    for forbidden in (
        "AITUNNEL_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "MODEL_ROUTER_MASTER_KEY",
        "MODEL_ROUTER_CLIENT_KEY",
        "REPO_MANAGER_AUTH_PROFILES_JSON",
    ):
        assert forbidden not in runner_env
    for marker in (
        "with_for_update(skip_locked=True)",
        "lease_generation",
        "RunnerOutcomeUnknown",
        "repository_trust_revoked",
        "runner_job.authorized",
        "runner_identity_mismatch",
        "AF_UNIX",
    ):
        assert marker in runner_manager_text, (
            f"Runner Manager safety marker missing: {marker}"
        )
    assert "subprocess" not in runner_manager_text
    assert "/var/run/docker.sock" not in runner_manager_text
    assert "shell=True" not in runner_manager_text
    base_compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "runner-manager:" not in base_compose_text
    runner_overlay_text = (
        ROOT / "deploy/docker-compose.runner-manager.yml"
    ).read_text(encoding="utf-8")
    assert "RUNNERD_SOCKET_GID:?" in runner_overlay_text
    assert "RUNNERD_SOCKET_HOST_PATH:?" in runner_overlay_text


    smoke_script = (ROOT / "scripts/smoke.sh").read_text(encoding="utf-8")
    assert "gateway_ip=" in smoke_script
    assert "docker compose ps --status running -q model-gateway" in smoke_script
    assert "docker compose exec -T opencode" in smoke_script

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

    token_caps = {
        "orchestra-lead": 3072,
        "orchestra-reviewer": 3072,
        "orchestra-risk": 2048,
        "orchestra-architect": 3072,
        "orchestra-coder": 6144,
        "orchestra-quant": 3072,
        "orchestra-analyst": 3072,
        "orchestra-fast": 1536,
        "orchestra-qa": 3072,
    }
    for router_path in (
        ROOT / "config/model-router.shared.yaml",
        ROOT / "config/model-router.separate.yaml",
    ):
        router = yaml.safe_load(router_path.read_text(encoding="utf-8"))
        observed = {
            item["model_name"]: item.get("litellm_params", {}).get("max_tokens")
            for item in router["model_list"]
        }
        assert observed == token_caps, (router_path, observed)

    opencode = json.loads((ROOT / "config/opencode.gateway.json").read_text(encoding="utf-8"))
    assert opencode.get("compaction") == {
        "auto": True,
        "prune": True,
        "reserved": 12000,
    }

    print("[OK] static security boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
