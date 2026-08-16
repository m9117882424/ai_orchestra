.PHONY: init shared separate build up down restart logs manager-logs status preflight smoke validate test backup

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
	docker compose up -d --force-recreate opencode control-plane

logs:
	docker compose logs -f --tail=200 opencode control-plane postgres

manager-logs:
	docker compose logs -f --tail=200 control-plane

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
	python3 -m json.tool config/opencode.shared.json >/dev/null
	python3 -m json.tool config/opencode.separate.json >/dev/null
	python3 -m compileall -q control_plane/app control_plane/tests
	docker compose config --quiet
