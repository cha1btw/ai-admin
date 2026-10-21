.DEFAULT_GOAL := help
FILE ?= studios/demo_studio.json
TENANT ?= demo_studio

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' Makefile | awk 'BEGIN {FS = ":.*## "} {printf "  %-16s %s\n", $$1, $$2}'

install: ## Install dependencies into .venv
	uv sync

db: ## Start Postgres in Docker (localhost:5435)
	docker compose up -d --wait db

migrate: db ## Apply database migrations
	uv run alembic upgrade head

run: migrate ## API with auto-reload on :8000
	uv run uvicorn app.api.main:app --reload --port 8000

admin: ## Login link to the admin panel (open it while `make run` is running)
	uv run python -m app.cli admin link

worker: migrate ## Worker: inbound queue, replies, periodic jobs
	uv run python -m app.worker.main

poll: ## Development only: fetch Telegram updates by polling instead of webhooks
	uv run python -m app.worker.telegram_poll

test: db ## Unit and integration tests (no API keys needed)
	uv run pytest

lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Format and auto-fix
	uv run ruff format .
	uv run ruff check --fix .

eval: migrate ## Evals with the real model (needs OPENAI_API_KEY and LLM_MODEL); ARGS="--runs 3"
	uv run python -m tests.evals.runner $(ARGS)

chat: migrate ## Talk to a studio bot in the terminal: make chat TENANT=demo_studio
	uv run python -m app.cli chat $(TENANT)

demo: migrate ## Load the demo studio into the database
	uv run python -m app.cli tenant load studios/demo_studio.json --demo

tenant-validate: ## Validate a studio file: make tenant-validate FILE=studios/private/x.json
	uv run python -m app.cli tenant validate $(FILE)

tenant-load: migrate ## Load or update a studio: make tenant-load FILE=studios/private/x.json
	uv run python -m app.cli tenant load $(FILE)

tenant-preview: ## Show what the bot knows about a studio: make tenant-preview TENANT=demo_studio
	uv run python -m app.cli tenant preview $(TENANT)

webhooks: ## Production: point Telegram bots at PUBLIC_BASE_URL
	uv run python -m app.cli telegram set-webhooks

backup: ## Production: database backup (see deploy/backup.sh)
	deploy/backup.sh

.PHONY: help install db migrate run admin worker poll test lint fmt eval chat demo tenant-validate tenant-load tenant-preview webhooks backup
