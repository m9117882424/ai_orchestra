#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

failures=0
pass() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1"; failures=$((failures + 1)); }

for command_name in docker python3 flock; do
  if command -v "$command_name" >/dev/null 2>&1; then
    pass "Команда доступна: $command_name"
  else
    fail "Не найдена команда: $command_name"
  fi
done

if docker compose version >/dev/null 2>&1; then
  pass "Docker Compose v2 доступен"
else
  fail "Docker Compose v2 недоступен"
fi

for secret_file in .env .env.providers .env.repositories; do
  if [[ -f "$secret_file" ]]; then
    pass "$secret_file существует"
    mode="$(stat -c '%a' "$secret_file" 2>/dev/null || true)"
    if [[ "$mode" == "600" ]]; then
      pass "$secret_file имеет права 600"
    else
      fail "$secret_file должен иметь права 600 (сейчас: ${mode:-unknown})"
    fi
  else
    fail "$secret_file не найден; выполните make init"
  fi
done

if [[ -f .env.repositories ]]; then
  if python3 - .env.repositories <<'PY'
import json
import ipaddress
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
values = {}
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit(1)
    key, value = line.split("=", 1)
    key = key.strip()
    if key in values:
        raise SystemExit(1)
    values[key] = value.strip()
if set(values) != {"REPO_MANAGER_AUTH_PROFILES_JSON"}:
    raise SystemExit(1)
raw = values["REPO_MANAGER_AUTH_PROFILES_JSON"]
if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
    raw = raw[1:-1]
if len(raw) > 65536:
    raise SystemExit(1)
payload = json.loads(raw)
if not isinstance(payload, dict):
    raise SystemExit(1)
profile_re = re.compile(r"[a-z0-9](?:[a-z0-9._:-]{0,78}[a-z0-9])?")
secret_like_prefixes = (
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "glpat-", "sk-", "xoxb-", "xoxp-",
)
host_re = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
reserved = (".example", ".internal", ".invalid", ".local", ".localhost", ".test")
for name, profile in payload.items():
    if (
        not isinstance(name, str)
        or not profile_re.fullmatch(name)
        or name.startswith(secret_like_prefixes)
    ):
        raise SystemExit(1)
    if not isinstance(profile, dict) or set(profile) != {"host", "username", "password"}:
        raise SystemExit(1)
    host = profile["host"]
    username = profile["username"]
    password = profile["password"]
    if not all(isinstance(value, str) for value in (host, username, password)):
        raise SystemExit(1)
    if host != host.lower() or host.endswith(".") or "." not in host or host.endswith(reserved):
        raise SystemExit(1)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SystemExit(1)
    if any(not host_re.fullmatch(label) for label in host.split(".")):
        raise SystemExit(1)
    if not (1 <= len(username) <= 256 and username.isprintable()):
        raise SystemExit(1)
    if not (1 <= len(password) <= 4096 and password.isprintable()):
        raise SystemExit(1)
PY
  then
    pass ".env.repositories содержит изолированный JSON auth profiles"
  else
    fail ".env.repositories должен содержать только валидный REPO_MANAGER_AUTH_PROFILES_JSON object"
  fi
fi

if [[ -f runtime/opencode.json ]] && python3 -m json.tool runtime/opencode.json >/dev/null; then
  pass "Активная конфигурация OpenCode валидна"
else
  fail "runtime/opencode.json отсутствует или содержит ошибку"
fi

if [[ -s runtime/model-router.yaml ]]; then
  pass "Конфигурация Model Router существует"
else
  fail "runtime/model-router.yaml отсутствует"
fi

if [[ -f .env && -f .env.providers && -f .env.repositories ]]; then
  if grep -Eq '^(AITUNNEL_API_KEY|AITUNNEL_BASE_URL|OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_GENERATIVE_AI_API_KEY)=' .env; then
    fail "Provider credentials/config обнаружены в .env; перенесите строки в .env.providers и удалите их из .env"
  else
    pass "Provider credentials изолированы от .env"
  fi

  if grep -Eq '^(REPO_MANAGER_AUTH_PROFILES_JSON)=' .env .env.providers; then
    fail "Git credentials обнаружены вне .env.repositories"
  else
    pass "Git credentials изолированы в Repo Manager scope"
  fi

  if grep -Eq '^(AITUNNEL_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_GENERATIVE_AI_API_KEY|CONTROL_PLANE_DB_PASSWORD|CONTROL_PLANE_SERVER_PASSWORD|MODEL_ROUTER_MASTER_KEY|MODEL_ROUTER_CLIENT_KEY)=' .env.repositories; then
    fail ".env.repositories содержит посторонние credentials"
  else
    pass "Repo Manager credential scope не содержит AI/admin/DB secrets"
  fi

  set -a
  # shellcheck disable=SC1091
  source .env
  # shellcheck disable=SC1091
  source .env.providers
  set +a

  OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-}"
  CONTROL_PLANE_SERVER_PASSWORD="${CONTROL_PLANE_SERVER_PASSWORD:-}"
  CONTROL_PLANE_DB_PASSWORD="${CONTROL_PLANE_DB_PASSWORD:-}"
  MODEL_ROUTER_MASTER_KEY="${MODEL_ROUTER_MASTER_KEY:-}"
  MODEL_ROUTER_CLIENT_KEY="${MODEL_ROUTER_CLIENT_KEY:-}"
  execution_timeout_seconds="${CONTROL_PLANE_EXECUTION_TIMEOUT_SECONDS:-7200}"

  if [[ "$execution_timeout_seconds" =~ ^[0-9]+$ \
    && "${#execution_timeout_seconds}" -le 6 ]] \
    && (( 10#$execution_timeout_seconds >= 60 \
      && 10#$execution_timeout_seconds <= 604800 )); then
    pass "Deadline выполнения задан: ${execution_timeout_seconds}s"
  else
    fail "CONTROL_PLANE_EXECUTION_TIMEOUT_SECONDS должен быть целым числом от 60 до 604800"
  fi

  if python3 - <<'PY'
import os

rules = {
    "REPO_MANAGER_LEASE_SECONDS": (300, 120, 3600),
    "REPO_MANAGER_GIT_TIMEOUT_SECONDS": (60, 10, 600),
    "REPO_MANAGER_REFRESH_SECONDS": (3600, 60, 604800),
    "REPO_MANAGER_RETRY_BASE_SECONDS": (30, 5, 3600),
    "REPO_MANAGER_RETRY_MAX_SECONDS": (1800, 5, 86400),
    "REPO_MANAGER_MAX_ACTIVE": (1, 1, 8),
    "REPO_MANAGER_POLL_SECONDS": (10, 1, 60),
    "REPO_MANAGER_MAX_REPOSITORY_BYTES": (5368709120, 1048576, 1099511627776),
    "REPO_MANAGER_MIN_FREE_BYTES": (536870912, 67108864, 1099511627776),
    "WORKSPACE_MANAGER_LEASE_SECONDS": (180, 60, 3600),
    "WORKSPACE_MANAGER_GIT_TIMEOUT_SECONDS": (60, 10, 600),
    "WORKSPACE_MANAGER_RETRY_BASE_SECONDS": (15, 1, 3600),
    "WORKSPACE_MANAGER_RETRY_MAX_SECONDS": (900, 1, 86400),
    "WORKSPACE_MANAGER_MAX_PREPARE_FAILURES": (8, 1, 100),
    "WORKSPACE_MANAGER_MAX_ACTIVE": (1, 1, 4),
    "WORKSPACE_MANAGER_POLL_SECONDS": (5, 1, 60),
    "WORKSPACE_MANAGER_MAX_FILES": (100000, 1, 1000000),
    "WORKSPACE_MANAGER_MAX_FILE_BYTES": (134217728, 1024, 1073741824),
    "WORKSPACE_MANAGER_MAX_WORKSPACE_BYTES": (5368709120, 1048576, 1099511627776),
    "WORKSPACE_MANAGER_MIN_FREE_BYTES": (536870912, 67108864, 1099511627776),
}
values = {}
for name, (default, minimum, maximum) in rules.items():
    raw = os.getenv(name, str(default))
    if not raw.isascii() or not raw.isdecimal():
        raise SystemExit(1)
    value = int(raw)
    if not minimum <= value <= maximum:
        raise SystemExit(1)
    values[name] = value
if values["REPO_MANAGER_LEASE_SECONDS"] < values["REPO_MANAGER_GIT_TIMEOUT_SECONDS"] + 30:
    raise SystemExit(1)
if values["REPO_MANAGER_RETRY_MAX_SECONDS"] < values["REPO_MANAGER_RETRY_BASE_SECONDS"]:
    raise SystemExit(1)
if values["WORKSPACE_MANAGER_LEASE_SECONDS"] < values["WORKSPACE_MANAGER_GIT_TIMEOUT_SECONDS"] + 30:
    raise SystemExit(1)
if values["WORKSPACE_MANAGER_RETRY_MAX_SECONDS"] < values["WORKSPACE_MANAGER_RETRY_BASE_SECONDS"]:
    raise SystemExit(1)
PY
  then
    pass "Repo/Workspace Manager runtime limits и lease/timeout invariants валидны"
  else
    fail "Некорректны Repo/Workspace Manager limits или lease меньше Git timeout + 30s"
  fi

  case "${KEY_MODE:-}" in
    shared)
      if [[ -n "${AITUNNEL_API_KEY:-}" ]]; then
        pass "AITunnel-ключ задан в provider scope"
      else
        fail "Для shared заполните AITUNNEL_API_KEY в .env.providers"
      fi
      ;;
    separate)
      if [[ -n "${OPENAI_API_KEY:-}" ]]; then
        pass "OpenAI-ключ задан"
      else
        fail "Для separate заполните OPENAI_API_KEY в .env.providers"
      fi
      if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
        pass "Anthropic-ключ задан"
      else
        fail "Для separate заполните ANTHROPIC_API_KEY в .env.providers"
      fi
      if [[ -n "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]]; then
        pass "Google-ключ задан"
      else
        fail "Для separate заполните GOOGLE_GENERATIVE_AI_API_KEY в .env.providers"
      fi
      ;;
    *)
      fail "KEY_MODE должен быть shared или separate"
      ;;
  esac

  if [[ "${KEY_MODE:-}" == "shared" || "${KEY_MODE:-}" == "separate" ]]; then
    if cmp -s runtime/model-router.yaml "config/model-router.${KEY_MODE}.yaml"; then
      pass "runtime/model-router.yaml соответствует KEY_MODE=${KEY_MODE}"
    else
      fail "runtime/model-router.yaml не соответствует KEY_MODE=${KEY_MODE}; выполните ./scripts/switch-key-mode.sh ${KEY_MODE} --no-restart или make init"
    fi
  fi

  if cmp -s runtime/opencode.json config/opencode.gateway.json; then
    pass "runtime/opencode.json соответствует gateway-конфигурации"
  else
    fail "runtime/opencode.json устарел; выполните make init"
  fi

  if [[ "${#OPENCODE_SERVER_PASSWORD}" -ge 20 && "$OPENCODE_SERVER_PASSWORD" != "CHANGE_ME_LONG_RANDOM_PASSWORD" ]]; then
    pass "Пароль OpenCode задан"
  else
    fail "OPENCODE_SERVER_PASSWORD должен содержать не менее 20 символов"
  fi

  if [[ "${#CONTROL_PLANE_SERVER_PASSWORD}" -ge 20 && "$CONTROL_PLANE_SERVER_PASSWORD" != "CHANGE_ME_MANAGER_PASSWORD" ]]; then
    pass "Пароль кабинета руководителя задан"
  else
    fail "CONTROL_PLANE_SERVER_PASSWORD должен содержать не менее 20 символов"
  fi

  if [[ "${#CONTROL_PLANE_DB_PASSWORD}" -ge 20 && "$CONTROL_PLANE_DB_PASSWORD" != "CHANGE_ME_DATABASE_PASSWORD" ]]; then
    pass "Пароль PostgreSQL кабинета задан"
  else
    fail "CONTROL_PLANE_DB_PASSWORD должен содержать не менее 20 символов"
  fi

  if [[ "${#MODEL_ROUTER_MASTER_KEY}" -ge 32 && "$MODEL_ROUTER_MASTER_KEY" == sk-admin-* && "$MODEL_ROUTER_MASTER_KEY" != "CHANGE_ME_MODEL_ROUTER_MASTER_KEY" ]]; then
    pass "Admin key Model Router задан"
  else
    fail "MODEL_ROUTER_MASTER_KEY должен начинаться с sk-admin- и быть не короче 32 символов"
  fi

  if [[ "${#MODEL_ROUTER_CLIENT_KEY}" -ge 32 && "$MODEL_ROUTER_CLIENT_KEY" == sk-client-* && "$MODEL_ROUTER_CLIENT_KEY" != "CHANGE_ME_MODEL_ROUTER_CLIENT_KEY" ]]; then
    pass "Inference client key задан"
  else
    fail "MODEL_ROUTER_CLIENT_KEY должен начинаться с sk-client- и быть не короче 32 символов"
  fi

  if [[ "$MODEL_ROUTER_MASTER_KEY" != "$MODEL_ROUTER_CLIENT_KEY" ]]; then
    pass "Router admin/client credentials разделены"
  else
    fail "Router admin/client credentials не должны совпадать"
  fi
fi

if docker compose config --quiet >/dev/null 2>&1; then
  pass "docker-compose.yml валиден"
else
  fail "docker compose config завершился с ошибкой"
fi

# Verify OS-level isolation using Compose's resolved JSON without printing secrets.
if docker compose config --format json 2>/dev/null | python3 -c '
import json, sys
cfg=json.load(sys.stdin)
services=cfg["services"]
op=services["opencode"]
forbidden={"AITUNNEL_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY","GOOGLE_GENERATIVE_AI_API_KEY","CONTROL_PLANE_DB_PASSWORD","CONTROL_PLANE_SERVER_PASSWORD","MODEL_ROUTER_MASTER_KEY"}
env=set((op.get("environment") or {}).keys())
bad=sorted(env & forbidden)
if bad:
    raise SystemExit("OpenCode receives forbidden secrets: " + ", ".join(bad))
assert "MODEL_ROUTER_CLIENT_KEY" in env

def nets(name):
    n=services[name].get("networks") or {}
    return set(n.keys() if isinstance(n, dict) else n)

assert nets("postgres")=={"control-db"}, nets("postgres")
assert nets("control-plane")=={"control-db","control-access","model-net"}, nets("control-plane")
assert nets("execution-worker")=={"control-db","model-net"}, nets("execution-worker")
assert nets("repo-manager")=={"control-db","repository-egress"}, nets("repo-manager")
assert nets("workspace-manager")=={"control-db"}, nets("workspace-manager")
assert nets("opencode")=={"model-net"}, nets("opencode")
assert nets("model-gateway")=={"model-net","router-backend"}, nets("model-gateway")
assert nets("model-router")=={"router-backend","provider-egress"}, nets("model-router")
assert not (nets("opencode") & nets("model-router")), "OpenCode must not share a network with router admin service"
assert not (nets("repo-manager") & nets("opencode")), "Repo Manager must not share an OpenCode network"
assert not (nets("repo-manager") & nets("model-router")), "Repo Manager must not share a router network"
assert not (nets("workspace-manager") & nets("opencode")), "Workspace Manager must not share an OpenCode network"
assert not (nets("workspace-manager") & nets("model-router")), "Workspace Manager must not share a router network"
worker=services["execution-worker"]
assert not worker.get("ports"), "Execution worker must not publish ports"
worker_mounts=worker.get("volumes") or []
assert len(worker_mounts)==1, worker_mounts
assert worker_mounts[0].get("target")=="/workspace/worktrees/managed", worker_mounts
assert worker_mounts[0].get("read_only") is True, worker_mounts
worker_env=set((worker.get("environment") or {}).keys())
forbidden_worker={"AITUNNEL_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY","GOOGLE_GENERATIVE_AI_API_KEY","MODEL_ROUTER_MASTER_KEY","MODEL_ROUTER_CLIENT_KEY"}
assert not (worker_env & forbidden_worker), "Execution worker receives model/provider credentials"
repo=services["repo-manager"]
assert not repo.get("ports"), "Repo Manager must not publish ports"
assert repo.get("pids_limit")==64, "Repo Manager must have a PID limit"
assert repo.get("image") != services["control-plane"].get("image"), "Repo Manager must use a separate image"
repo_env=repo.get("environment") or {}
assert "REPO_MANAGER_AUTH_PROFILES_JSON" in repo_env
assert repo_env.get("CONTROL_PLANE_SERVER_PASSWORD")=="repo-manager-does-not-use-manager-auth"
assert repo_env.get("CONTROL_PLANE_OPENCODE_PASSWORD")=="repo-manager-does-not-use-opencode-auth"
for name, service in services.items():
    if name != "repo-manager":
        assert "REPO_MANAGER_AUTH_PROFILES_JSON" not in (service.get("environment") or {}), name
mounts=repo.get("volumes") or []
assert len(mounts)==1 and mounts[0].get("target")=="/var/lib/ai-orchestra/repositories", mounts
workspace=services["workspace-manager"]
assert not workspace.get("ports"), "Workspace Manager must not publish ports"
workspace_env=workspace.get("environment") or {}
assert workspace_env.get("CONTROL_PLANE_SERVER_PASSWORD")=="workspace-manager-does-not-use-manager-auth"
assert workspace_env.get("CONTROL_PLANE_OPENCODE_PASSWORD")=="workspace-manager-does-not-use-opencode-auth"
for forbidden in forbidden_worker | {"REPO_MANAGER_AUTH_PROFILES_JSON"}:
    assert forbidden not in workspace_env, forbidden
workspace_mounts=workspace.get("volumes") or []
assert {(item.get("target"), bool(item.get("read_only"))) for item in workspace_mounts}=={
    ("/var/lib/ai-orchestra/repositories", True),
    ("/workspace/worktrees/managed", False),
}, workspace_mounts
opencode_mounts=services["opencode"].get("volumes") or []
task_mount=[item for item in opencode_mounts if item.get("target")=="/workspace/worktrees/managed"]
assert len(task_mount)==1 and task_mount[0].get("read_only") is not True, task_mount
assert set(op.get("cap_drop") or [])=={"ALL"}
assert set(op.get("cap_add") or [])=={"DAC_OVERRIDE","FOWNER"}
manual_mount=[item for item in opencode_mounts if item.get("target")=="/workspace/worktrees/manual"]
assert len(manual_mount)==1 and manual_mount[0].get("type")=="bind", manual_mount
assert not any(item.get("target")=="/workspace/worktrees" for item in opencode_mounts), (
    "A parent /workspace/worktrees mount would mask the managed workspace volume"
)
volume_init=services["workspace-volume-init"]
assert volume_init.get("network_mode")=="none"
assert volume_init.get("user")=="0:0"
assert volume_init.get("read_only") is True
assert not volume_init.get("environment")
assert set(volume_init.get("cap_add") or [])=={"CHOWN","DAC_OVERRIDE","FOWNER"}
assert set(volume_init.get("cap_drop") or [])=={"ALL"}
init_entrypoint=volume_init.get("entrypoint") or []
assert init_entrypoint==["/app/app/workspace_volume_init.sh"], init_entrypoint
assert not volume_init.get("command"), volume_init.get("command")
init_mounts=volume_init.get("volumes") or []
assert len(init_mounts)==1 and init_mounts[0].get("target")=="/workspace/worktrees/managed", init_mounts
assert (workspace.get("depends_on") or {})["workspace-volume-init"]["condition"]=="service_completed_successfully"
assert (services["opencode"].get("depends_on") or {})["workspace-volume-init"]["condition"]=="service_completed_successfully"
' >/dev/null; then
  pass "Секреты, сети и task workspace mounts изолированы по ролям"
else
  fail "Нарушена изоляция сервисов, credential boundary или task workspace mounts"
fi

if (( failures > 0 )); then
  echo
  echo "Preflight: обнаружено ошибок: $failures"
  exit 1
fi

echo
echo "Preflight: контур готов к сборке."
