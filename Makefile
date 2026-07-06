COMPOSE := docker compose
VIGIA_HOST ?= user@homelab       # override for remote deploy

.DEFAULT_GOAL := help

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

## --- Dev (local, on your Mac) ---
# Only what the build needs (pyproject + package): caches/tests/lock would
# bust the Docker dependency layer on every local test run.
vendor-core:  ## Refresh the vendored radar-core snapshot (private lib)
	rsync -a --delete \
		--exclude '.git' --exclude '.venv' --exclude 'tests' \
		--exclude 'uv.lock' --exclude '__pycache__' --exclude '.*_cache' \
		--exclude '*.md' \
		../radar-core/ vendor/radar-core/

install: vendor-core  ## Sync deps locally
	uv sync

dev:  ## Run the scheduler locally (foreground)
	uv run python -m vigia

tick:  ## Run a single tick (manual test, no daemon)
	uv run python -m vigia.tick

db-init:  ## Apply schema to the DB
	uv run python -m vigia.db init

seed:  ## Load curated routes into the DB
	uv run python -m vigia.seed

test:  ## Run tests
	uv run pytest -q

lint:  ## Lint + typecheck
	uv run ruff check . && uv run mypy vigia

fmt:  ## Format
	uv run ruff format .

## --- Docker (homelab) ---
build: vendor-core  ## Build the image
	$(COMPOSE) build

up:  ## Start the daemon (detached)
	$(COMPOSE) up -d

down:  ## Stop
	$(COMPOSE) down

logs:  ## Follow logs
	$(COMPOSE) logs -f vigia

shell:  ## Shell into the running container
	$(COMPOSE) exec vigia bash

docker-init:  ## Init DB inside the container
	$(COMPOSE) run --rm vigia uv run python -m vigia.db init

## --- Deploy (remote Proxmox host) ---
deploy: vendor-core  ## rsync + rebuild + up on VIGIA_HOST
	rsync -az --delete --exclude '.git' --exclude '.env' --exclude '.venv' ./ $(VIGIA_HOST):~/vigia/
	ssh $(VIGIA_HOST) 'cd ~/vigia && docker compose up -d --build'
