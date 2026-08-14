# CloudOps Multi-Agent System -- Makefile
#
# Common commands for development, testing, and deployment.
# Run `make help` to see all available targets.

.PHONY: help setup configure quickstart reconfigure-shared nuke-shared-config test test-unit test-integration test-topology test-scripts lint plan deploy deploy-auto destroy destroy-all package build-agents clean

SHELL := /bin/bash

# NOTE: we deliberately do NOT `-include .env` + `export` here. Make's
# default behaviour is that Makefile-level assignments beat inherited
# environment variables, which meant `AWS_REGION=eu-west-1 make deploy-auto`
# silently got overridden by whatever .env held. All downstream scripts
# (deploy.sh, run-local.sh, invoke_agent.py) source .env themselves and
# correctly honour CLI overrides.

VENV := .venv/bin
PYTHON := $(VENV)/python
PYTEST := $(PYTHON) -m pytest
PIP := $(VENV)/pip
HASH_DIR := .lambda-hashes

# Default target
help: ## Show this help message
	@echo "CloudOps Multi-Agent System"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(firstword $(MAKEFILE_LIST)) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup / Configure
# ---------------------------------------------------------------------------
setup: ## Interactive identity setup (.env) + install Python deps
	@./scripts/deploy.sh --setup

configure: ## First-run shared project config (writes to SSM via shared-config module)
	@./scripts/deploy.sh --configure

quickstart: ## First-time deploy: setup + configure + deploy in one command
	@echo "══════════════════════════════════════════════════════════"
	@echo "  CloudOps Multi-Agent System — Quick Start"
	@echo "══════════════════════════════════════════════════════════"
	@echo ""
	@echo "Step 1/3: Setting up local environment..."
	@$(MAKE) setup
	@echo ""
	@echo "Step 2/3: Configuring shared deployment settings..."
	@$(MAKE) configure
	@echo ""
	@echo "Step 3/3: Deploying platform (this takes ~10 minutes)..."
	@$(MAKE) deploy-auto
	@echo ""
	@echo "══════════════════════════════════════════════════════════"
	@echo "  Done! Your platform is live at the CloudFront URL above."
	@echo "  Run 'make run-local' for local development."
	@echo "══════════════════════════════════════════════════════════"

reconfigure-shared: ## Change shared config with diff + APPLY CHANGES confirmation
	@./scripts/deploy.sh --reconfigure-shared

nuke-shared-config: ## Delete every SSM parameter under /$$PROJECT_PREFIX/$$ENVIRONMENT/config (typed confirmation required)
	@./scripts/deploy.sh --nuke-shared-config

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
test: ## Run all unit + topology tests (no AWS, ~20s)
	$(PYTEST) tests/unit/ tests/topology/ -v

test-unit: ## Run unit + topology tests (no AWS, ~20s)
	$(PYTEST) tests/unit/ tests/topology/ -v

test-integration: ## Run integration tests against live stack (requires deployed stack + Cognito creds)
	$(PYTEST) tests/integration/ -v --tb=short

test-topology: ## Run ONLY the topology tests (slice + terraform validate, no AWS)
	$(PYTEST) tests/topology/ -v --tb=short

test-scripts: ## Run shell-level unit tests for scripts/lib/* (auth.sh, cmd_setup, ...)
	@for t in tests/scripts/test_*.sh; do echo "==> $$t"; bash "$$t" || exit 1; done

# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------
plan: ## Terraform plan only (no apply)
	./scripts/deploy.sh --all --plan

deploy: ## Interactive deployment (prompts for what to deploy)
	./scripts/deploy.sh

deploy-auto: package ## Non-interactive full deploy (CI-friendly, no prompts)
	./scripts/deploy.sh --all --auto

destroy: ## Destroy infrastructure (with confirmation)
	./scripts/deploy.sh --destroy

destroy-all: ## Full teardown: infra + ECR + memory + state backend (with confirmation)
	./scripts/deploy.sh --destroy-all

build-agents: ## Build container images for all resolved agents
	./scripts/deploy.sh --build-agents-only

run-local: ## Run frontend locally with Cognito auth (fetches config from Terraform)
	./scripts/run-local.sh

run-local-bypass: ## Run frontend locally without auth (dev bypass)
	./scripts/run-local.sh --bypass-auth

# ---------------------------------------------------------------------------
# Health events backfill (optional, needs Business+ AWS Support)
# ---------------------------------------------------------------------------
# DAYS          window in days (default 30, max 90 — AWS Health retention).
# ORG           set to 1 or true to use DescribeEventsForOrganization
#               (requires Health org view enabled + run from mgmt/delegated admin).
# ROLE_ARN      optional IAM role to assume before calling Health/Organizations.
# DRY_RUN       set to 1 or true to list events without writing to DynamoDB.
#
# Examples:
#   make backfill-health DAYS=30
#   make backfill-health DAYS=90 ORG=1
#   make backfill-health DAYS=14 ORG=1 ROLE_ARN=arn:aws:iam::111122223333:role/HealthBackfill
#   make backfill-health DAYS=30 DRY_RUN=1
backfill-health: ## Backfill AWS Health events (needs Business+ Support)
	@.venv/bin/python scripts/backfill_health.py \
		--days $(or $(DAYS),30) \
		$(if $(filter 1 true True TRUE,$(ORG)),--org,) \
		$(if $(ROLE_ARN),--role-arn $(ROLE_ARN),) \
		$(if $(filter 1 true True TRUE,$(DRY_RUN)),--dry-run,)

# ---------------------------------------------------------------------------
# Lambda Packaging -- hash-based rebuild (skip unchanged tools)
# ---------------------------------------------------------------------------
LAMBDA_DIRS := $(wildcard src/lambda/mcp/*/)
LAMBDA_TOOLS := $(patsubst src/lambda/mcp/%/,%,$(LAMBDA_DIRS))

$(HASH_DIR):
	@mkdir -p $(HASH_DIR)

package: $(HASH_DIR) ## Package Lambda tools (only changed ones, parallel)
	@echo "Packaging Lambda tools..."
	@# The shared/ subdir is a helper package (cross_account etc.) copied into
	@# every tool zip — it has no standalone handler. Compute its hash once
	@# and fold it into each tool's hash so edits there force a rebuild of
	@# every tool (same idea as hierarchy.json triggering agent rebuilds).
	@shared_hash=$$(find src/lambda/mcp/shared -type f \( -name '*.py' -o -name '*.json' \) -exec shasum {} + 2>/dev/null | sort | shasum | cut -d' ' -f1); \
	_pids=""; _failed=0; \
	for dir in src/lambda/mcp/*/; do \
		tool=$$(basename "$$dir"); \
		if [ "$$tool" = "shared" ]; then continue; fi; \
		tool_hash=$$(find "$$dir" -type f \( -name '*.py' -o -name '*.txt' -o -name '*.json' \) -exec shasum {} + 2>/dev/null | sort | shasum | cut -d' ' -f1); \
		current_hash=$$(printf '%s%s' "$$tool_hash" "$$shared_hash" | shasum | cut -d' ' -f1); \
		stored_hash=$$(cat $(HASH_DIR)/$$tool.sha 2>/dev/null || echo ""); \
		if [ "$$current_hash" = "$$stored_hash" ] && [ -f "src/lambda/mcp/$$tool.zip" ]; then \
			echo "  $$tool: unchanged, skipping"; \
		else \
			( \
				echo "  Packaging $$tool..."; \
				rm -rf "$$dir/package"; \
				rm -f "src/lambda/mcp/$$tool.zip"; \
				mkdir -p "$$dir/package/"; \
				if grep -qvE '^[[:space:]]*(#|$$)' "$$dir/requirements.txt" 2>/dev/null; then \
					pip install -r "$$dir/requirements.txt" -t "$$dir/package/" \
						--platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
						--python-version 3.12 --implementation cp \
						--only-binary=:all: --upgrade --quiet || exit 1; \
				fi; \
				cp $$dir/*.py "$$dir/package/" 2>/dev/null; \
				for sub in "$$dir"*/; do \
					subname=$$(basename "$$sub"); \
					[ -d "$$sub" ] && [ "$$subname" != "package" ] && [ "$$subname" != "__pycache__" ] && cp -R "$${sub%/}" "$$dir/package/" 2>/dev/null; \
				done; \
				cp -R src/lambda/mcp/shared "$$dir/package/"; \
				(cd "$$dir/package" && zip -r "../../$$tool.zip" . -x '*.pyc' '*/__pycache__/*' --quiet 2>/dev/null); \
				rm -rf "$$dir/package"; \
				echo "$$current_hash" > $(HASH_DIR)/$$tool.sha; \
				echo "  $$tool: done"; \
			) & \
			_pids="$$_pids $$!"; \
		fi; \
	done; \
	for _pid in $$_pids; do \
		wait $$_pid || _failed=1; \
	done; \
	if [ "$$_failed" = "1" ]; then echo "ERROR: Lambda packaging failed"; exit 1; fi
	@echo "Lambda packaging complete."
	@# Package frontend (browser-facing REST) Lambdas. Each subdir under
	@# src/lambda/frontend/ is packaged independently. core-api has its own
	@# assets (report_templates/); network-resilience imports the shared
	@# network_resilience/ package copied in from src/lambda/mcp/network-resilience/.
	@for dir in src/lambda/frontend/*/; do \
		name=$$(basename "$$dir"); \
		zip_path="src/lambda/frontend/$$name.zip"; \
		hash_key="frontend-$$name"; \
		current_hash=$$(find "$$dir" -type f \( -name '*.py' -o -name '*.txt' -o -name '*.json' -o -name '*.yaml' -o -name '*.md' \) -exec shasum {} + 2>/dev/null | sort | shasum | cut -d' ' -f1); \
		if [ "$$name" = "network-resilience" ]; then \
			extra_hash=$$(find src/lambda/mcp/network-resilience/network_resilience -type f \( -name '*.py' -o -name '*.json' \) -exec shasum {} + 2>/dev/null | sort | shasum | cut -d' ' -f1); \
			current_hash=$$(printf '%s%s' "$$current_hash" "$$extra_hash" | shasum | cut -d' ' -f1); \
		fi; \
		stored_hash=$$(cat $(HASH_DIR)/$$hash_key.sha 2>/dev/null || echo ""); \
		if [ "$$current_hash" = "$$stored_hash" ] && [ -f "$$zip_path" ]; then \
			echo "  frontend/$$name: unchanged, skipping"; \
		else \
			echo "  Packaging frontend/$$name..."; \
			rm -rf "$$dir/package" "$$zip_path"; \
			mkdir -p "$$dir/package"; \
			if [ -f "$$dir/requirements.txt" ] && grep -qvE '^[[:space:]]*(#|$$)' "$$dir/requirements.txt" 2>/dev/null; then \
				pip install -r "$$dir/requirements.txt" -t "$$dir/package/" --quiet 2>/dev/null; \
			fi; \
			cp "$$dir"*.py "$$dir/package/" 2>/dev/null; \
			for sub in "$$dir"*/; do \
				subname=$$(basename "$$sub"); \
				[ -d "$$sub" ] && [ "$$subname" != "package" ] && [ "$$subname" != "__pycache__" ] && cp -R "$${sub%/}" "$$dir/package/" 2>/dev/null; \
			done; \
			if [ "$$name" = "network-resilience" ]; then \
				cp -R src/lambda/mcp/network-resilience/network_resilience "$$dir/package/"; \
			fi; \
			(cd "$$dir/package" && zip -r "../../../../../$$zip_path" . -x '*.pyc' '*/__pycache__/*' --quiet 2>/dev/null); \
			rm -rf "$$dir/package"; \
			echo "$$current_hash" > $(HASH_DIR)/$$hash_key.sha; \
			echo "  frontend/$$name: done"; \
		fi; \
	done
	@# Package collector Lambdas (EventBridge → SQS → DynamoDB background jobs).
	@# Terraform's `collector_zip_path` hashes each zip via `filebase64sha256`,
	@# so an unchanged zip means Lambda keeps running old code — hence this must
	@# land in `make package`, not be built by hand.
	@#
	@# `src/lambda/mcp/shared/` (cross-account helpers) is copied into each
	@# collector zip exactly like it is for MCP tools above — one source of
	@# truth for assume-role logic. Its hash folds into each collector's hash
	@# so edits there force all collectors to rebuild.
	@shared_hash=$$(find src/lambda/mcp/shared -type f \( -name '*.py' -o -name '*.json' \) -exec shasum {} + 2>/dev/null | sort | shasum | cut -d' ' -f1); \
	for dir in src/lambda/collectors/*/; do \
		name=$$(basename "$$dir"); \
		zip_path="$$dir$$name-collector.zip"; \
		hash_key="collector-$$name"; \
		src_hash=$$(find "$$dir" -type f \( -name '*.py' -o -name '*.txt' -o -name '*.json' \) ! -name "$$name-collector.zip" -exec shasum {} + 2>/dev/null | sort | shasum | cut -d' ' -f1); \
		current_hash=$$(printf '%s%s' "$$src_hash" "$$shared_hash" | shasum | cut -d' ' -f1); \
		stored_hash=$$(cat $(HASH_DIR)/$$hash_key.sha 2>/dev/null || echo ""); \
		if [ "$$current_hash" = "$$stored_hash" ] && [ -f "$$zip_path" ]; then \
			echo "  collector/$$name: unchanged, skipping"; \
		else \
			echo "  Packaging collector/$$name..."; \
			rm -rf "$$dir/package" "$$zip_path"; \
			mkdir -p "$$dir/package/"; \
			if [ -f "$$dir/requirements.txt" ] && grep -qvE '^[[:space:]]*(#|$$)' "$$dir/requirements.txt" 2>/dev/null; then \
				pip install -r "$$dir/requirements.txt" -t "$$dir/package/" --quiet 2>/dev/null; \
			fi; \
			cp "$$dir"*.py "$$dir/package/" 2>/dev/null; \
			cp -R src/lambda/mcp/shared "$$dir/package/"; \
			(cd "$$dir/package" && zip -r "../$$name-collector.zip" . -x '*.pyc' '*/__pycache__/*' --quiet 2>/dev/null); \
			rm -rf "$$dir/package"; \
			echo "$$current_hash" > $(HASH_DIR)/$$hash_key.sha; \
			echo "  collector/$$name: done"; \
		fi; \
	done

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
clean: ## Remove build artifacts, caches, and Lambda packages
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	rm -rf *.egg-info dist build
	rm -f src/lambda/mcp/*.zip
	rm -f src/lambda/frontend/*.zip
	rm -f src/lambda/collectors/*/*-collector.zip
	rm -rf src/lambda/mcp/*/package
	rm -rf src/lambda/collectors/*/package
	rm -rf src/lambda/frontend/*/package
	rm -rf $(HASH_DIR)
	rm -f src/agents/.hierarchy-*.json
	rm -rf src/frontend/out src/frontend/.next src/frontend/node_modules
	rm -f terraform/terraform.tfvars terraform/tfplan
	@echo "Cleaned."
