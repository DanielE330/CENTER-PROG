# Entry points for developers and the stand. `make help` lists them.
COMPOSE := docker compose -f deploy/compose/compose.yaml
PY := .venv-app/bin/python

.PHONY: help secrets gen up seed down logs ps test lint backtest e2e clean-volumes

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

secrets: ## Generate dev secrets once (deploy/dev-secrets, not in git)
	@mkdir -p deploy/dev-secrets && chmod 700 deploy/dev-secrets
	@for s in provisioner_password keycloak_admin_password gateway_client_secret counterparty_hmac_key; do \
	  [ -s deploy/dev-secrets/$$s ] || openssl rand -hex 24 > deploy/dev-secrets/$$s; done
	@[ -s deploy/dev-secrets/session_key ] || openssl rand -hex 32 > deploy/dev-secrets/session_key
	@[ -s deploy/dev-secrets/demo_password ] || echo "Demo-$$(openssl rand -hex 6)A1" > deploy/dev-secrets/demo_password
	@for s in openai_api_key anthropic_api_key gemini_api_key openrouter_api_key; do touch deploy/dev-secrets/$$s; done
	@chmod 644 deploy/dev-secrets/*
	@echo "dev secrets ready; demo password: $$(cat deploy/dev-secrets/demo_password)"

gen: ## Regenerate compose.yaml, nats.conf and Quadlet units from deploy/services.yaml
	$(PY) deploy/gen.py

up: secrets ## Build and start the whole stack (https://localhost:8443)
	$(COMPOSE) up -d --build
	@echo "open https://localhost:8443 (accept the local CA certificate)"

seed: ## Create the demo tenants from the dataset
	$(COMPOSE) --profile seed run --rm seed

down: ## Stop the stack (keeps database volumes)
	$(COMPOSE) down

clean-volumes: ## Stop and DELETE all stack data
	$(COMPOSE) down -v

ps: ## Container status
	$(COMPOSE) ps

logs: ## Follow logs
	$(COMPOSE) logs -f --tail=100

test: ## Unit tests (Rust and Python)
	cargo nextest run --workspace
	$(PY) -m pytest py/ci_common/tests services/categorizer/tests services/forecast/tests services/advisor/tests

lint: ## Linters and generated-file check
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings
	.venv-app/bin/ruff check py services ml deploy tests
	.venv-app/bin/ruff format --check py services ml deploy tests
	$(PY) deploy/gen.py --check

backtest: ## Backtest on the real dataset in data/ (local only)
	$(PY) ml/backtest/run.py --companies 30 --cutoffs 8

e2e: ## End-to-end checks against a running stack
	$(PY) -m pytest tests/e2e -q
