COMPOSE := DOCKER_BUILDKIT=1 docker compose -f ./infra/dev/docker-compose.yml

.PHONY: help init install lint type test run-script clean docker-up docker-stop docker-down docker-rebuild

help:
	@echo "  init          - Set up dev environment (one-time)"
	@echo "  install       - Install all dependencies"
	@echo "  lint          - Run ruff and black"
	@echo "  type          - Run pyright"
	@echo "  test          - Run pytest"
	@echo "  check         - Run lint, type, and test"
	@echo "  run-script    - Run a script: make run-script name=foo [args='...']"
	@echo "  clean         - Remove .venv and caches"
	@echo "  docker-up     - Start containers in background"
	@echo "  docker-stop   - Stop running containers"
	@echo "  docker-down   - Stop and remove containers"
	@echo "  docker-rebuild - Rebuild and start containers"

init: install
	@if [ ! -f ./infra/dev/.env ]; then \
		cp ./infra/dev/.env.example ./infra/dev/.env; \
		echo "Created infra/dev/.env -- please update it."; \
	fi
	poetry run pre-commit install

install:
	poetry install

lint:
	poetry run ruff check --fix .
	poetry run black .

type:
	poetry run pyright

test:
	@if [ -f infra/dev/.env ]; then \
		set -a; . infra/dev/.env; set +a; \
	fi; \
	poetry run pytest

check: lint type test

run-script:
	@if [ -z "$(name)" ]; then \
		echo "Usage: make run-script name=<script> [args='<args>']"; \
		exit 1; \
	fi
	set -a; . infra/dev/.env; set +a; \
	poetry run python scripts/$(name).py $(args)

clean:
	poetry env remove --all
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +

docker-up:
	$(COMPOSE) up -d

docker-stop:
	$(COMPOSE) stop

docker-down:
	$(COMPOSE) down

docker-rebuild:
	$(COMPOSE) up -d --build
