#!/usr/bin/env bash
set -Eeuo pipefail

TEMPORAL_SERVER_IMAGE="${TEMPORAL_POC_SERVER_IMAGE:-temporalio/auto-setup:1.29.7}"
POSTGRES_IMAGE="${TEMPORAL_POC_POSTGRES_IMAGE:-postgres:16-alpine}"
POSTGRES_PASSWORD="temporal-poc-only"
suffix="${GITHUB_RUN_ID:-local}-$$-$RANDOM"
network="ai-orchestra-temporal-poc-${suffix}"
postgres_container="ai-orchestra-temporal-poc-postgres-${suffix}"
temporal_container="ai-orchestra-temporal-poc-server-${suffix}"
state_dir="$(mktemp -d)"
evidence_path="${state_dir}/evidence.json"

cleanup() {
  set +e
  docker rm -f "${temporal_container}" >/dev/null 2>&1 || true
  docker rm -f "${postgres_container}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  rm -rf "${state_dir}"
}
trap cleanup EXIT INT TERM

wait_for_postgres() {
  local deadline=$((SECONDS + 90))
  while (( SECONDS < deadline )); do
    if docker logs "${postgres_container}" 2>&1 | grep -q 'PostgreSQL init process complete; ready for start up.' \
      && docker exec "${postgres_container}" psql -U temporal -d temporal -Atqc 'select 1' 2>/dev/null | grep -qx '1'; then
      return 0
    fi
    sleep 1
  done
  echo 'ERROR: disposable PostgreSQL did not become ready' >&2
  docker inspect "${postgres_container}" >&2 || true
  docker logs "${postgres_container}" >&2 || true
  return 1
}

host_port() {
  docker port "${temporal_container}" 7233/tcp | awk -F: 'NF {print $NF; exit}'
}

wait_for_temporal() {
  local address="$1"
  if ! python3 scripts/temporal_durable_poc.py wait --address "${address}" --timeout 120; then
    echo 'ERROR: disposable Temporal server did not become ready' >&2
    docker inspect "${temporal_container}" >&2 || true
    docker logs "${temporal_container}" >&2 || true
    return 1
  fi
}

echo "[INFO] Temporal PoC server image: ${TEMPORAL_SERVER_IMAGE}"
echo "[INFO] Temporal PoC PostgreSQL image: ${POSTGRES_IMAGE}"

docker network create "${network}" >/dev/null

docker run -d \
  --name "${postgres_container}" \
  --network "${network}" \
  --security-opt no-new-privileges:true \
  -e POSTGRES_USER=temporal \
  -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  -e POSTGRES_DB=temporal \
  "${POSTGRES_IMAGE}" >/dev/null
wait_for_postgres

docker run -d \
  --name "${temporal_container}" \
  --network "${network}" \
  --security-opt no-new-privileges:true \
  -p 127.0.0.1::7233 \
  -e DB=postgres12 \
  -e DB_PORT=5432 \
  -e POSTGRES_USER=temporal \
  -e POSTGRES_PWD="${POSTGRES_PASSWORD}" \
  -e POSTGRES_SEEDS="${postgres_container}" \
  -e TEMPORAL_ADDRESS="${temporal_container}:7233" \
  -e SKIP_DEFAULT_NAMESPACE_CREATION=false \
  "${TEMPORAL_SERVER_IMAGE}" >/dev/null

port="$(host_port)"
if [[ -z "${port}" ]]; then
  echo 'ERROR: Temporal host port was not published' >&2
  docker inspect "${temporal_container}" >&2 || true
  docker logs "${temporal_container}" >&2 || true
  exit 1
fi
address="127.0.0.1:${port}"

wait_for_temporal "${address}"
python3 scripts/temporal_durable_poc.py exercise \
  --address "${address}" \
  --state-dir "${state_dir}/run" \
  --evidence "${evidence_path}"

before_image_id="$(docker inspect --format '{{.Image}}' "${temporal_container}")"
docker restart "${temporal_container}" >/dev/null
wait_for_temporal "${address}"
python3 scripts/temporal_durable_poc.py verify --address "${address}" --evidence "${evidence_path}"
after_image_id="$(docker inspect --format '{{.Image}}' "${temporal_container}")"

if [[ "${before_image_id}" != "${after_image_id}" ]]; then
  echo 'ERROR: Temporal container image changed across restart' >&2
  exit 1
fi

python3 - "${evidence_path}" "${TEMPORAL_SERVER_IMAGE}" "${before_image_id}" <<'PY'
import json
import sys
from pathlib import Path

evidence_path = Path(sys.argv[1])
evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
evidence["temporal_server_image"] = sys.argv[2]
evidence["temporal_server_image_id"] = sys.argv[3]
evidence["temporal_server_restart"] = "verified"
evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(evidence, sort_keys=True))
PY

echo '[OK] Temporal durable-workflow PoC: worker-loss retry and server-restart recovery verified'
