.PHONY: init shared separate build up down restart logs status preflight smoke validate

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
	docker compose up -d --force-recreate opencode

logs:
	docker compose logs -f --tail=200 opencode

status:
	docker compose ps

preflight:
	./scripts/preflight.sh

smoke:
	./scripts/smoke.sh

validate:
	python3 -m json.tool config/opencode.shared.json >/dev/null
	python3 -m json.tool config/opencode.separate.json >/dev/null
	docker compose config --quiet

