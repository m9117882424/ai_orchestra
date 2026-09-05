.PHONY: init shared separate build up down restart logs manager-logs router-logs status preflight smoke validate test backup backup-verify backup-offsite restore-drill migrate schema-check dependency-check

init:
	./scripts/bootstrap.sh

shared:
	./scripts/switch-key-mode.sh shared

separate:
	./scripts/switch-key-mode.sh separate

build:
	docker compose build --pull

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose up -d --force-recreate model-router model-gateway opencode control-plane execution-worker

logs:
	docker compose logs -f --tail=200 model-router model-gateway opencode control-plane execution-worker postgres

manager-logs:
	docker compose logs -f --tail=200 control-plane execution-worker

router-logs:
	docker compose logs -f --tail=200 model-router model-gateway

status:
	docker compose ps

preflight:
	./scripts/preflight.sh

smoke:
	./scripts/smoke.sh

test:
	python3 -m pytest control_plane/tests

backup:
	./scripts/backup.sh

backup-verify:
	bash ./scripts/verify-backup.sh

backup-offsite:
	bash ./scripts/export-backup-offsite.sh

restore-drill:
	bash ./scripts/restore-drill.sh

migrate:
	bash ./scripts/migrate-control-plane.sh

schema-check:
	docker compose run --rm --no-deps control-plane python -m app.schema_cli check

dependency-check:
	python3 scripts/verify_dependency_locks.py

validate: dependency-check
	python3 -m json.tool config/opencode.gateway.json >/dev/null
	python3 -m compileall -q control_plane/app control_plane/migrations control_plane/tests scripts/model_router_smoke.py scripts/static_security_check.py scripts/verify_dependency_locks.py
	docker compose config --quiet
	python3 scripts/static_security_check.py
