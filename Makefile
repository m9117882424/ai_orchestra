.PHONY: init shared separate build up down restart logs manager-logs router-logs status preflight smoke validate test backup

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
	docker compose up -d --force-recreate model-router model-gateway opencode control-plane

logs:
	docker compose logs -f --tail=200 model-router model-gateway opencode control-plane postgres

manager-logs:
	docker compose logs -f --tail=200 control-plane

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

validate:
	python3 -m json.tool config/opencode.gateway.json >/dev/null
	python3 -m compileall -q control_plane/app control_plane/tests scripts/model_router_smoke.py scripts/static_security_check.py
	docker compose config --quiet
	python3 scripts/static_security_check.py
