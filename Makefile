.PHONY: install-dev install-orchestration dbt-deps dbt-run dbt-test dbt-docs dbt-clean dbt-parse lint test test-mcp build-image build-image-dagster run-api run-mcp db-init-analytics db-init-research db-init-ops-dagster db-migrate-6-4 db-migrate-6-6 dagster-dev dagster-defs dagster-ensure-schedule verify-husbandry-schedulers event-radar-ingest smoke-agents-sdk sync-openai-secret check-code-health install-hooks

DBT_DIR := src/bifrost_research/dbt
DBT_PROFILES := $(DBT_DIR)

install-dev:
	pip install -e ".[dev]"
	@$(MAKE) install-hooks

install-hooks:
	@git config core.hooksPath .githooks
	@chmod +x .githooks/pre-commit
	@echo "git hooksPath -> .githooks (code-health pre-commit)"

check-code-health:
	bash ../scripts/code-health/scan.sh --repo bifrost-research

install-orchestration:
	pip install -e ".[dev,orchestration]"

dbt-deps:
	cd $(DBT_DIR) && dbt deps --profiles-dir .

dbt-run:
	cd $(DBT_DIR) && $(if $(wildcard .venv/bin/dbt),.venv/bin/dbt,dbt) run --profiles-dir .

dbt-test:
	cd $(DBT_DIR) && dbt test --profiles-dir .

dbt-docs:
	cd $(DBT_DIR) && dbt docs generate --profiles-dir . && dbt docs serve --profiles-dir .

dbt-clean:
	cd $(DBT_DIR) && dbt clean

dbt-parse:
	cd $(DBT_DIR) && dbt parse --profiles-dir .

dagster-dev:
	dagster dev -w workspace.yaml

dagster-defs:
	python -c "from bifrost_research.orchestration.definitions import defs; print('assets=', len(defs.resolve_all_asset_keys()))"

# Instance DB may keep STOPPED even when DefaultScheduleStatus.RUNNING — flip explicitly.
dagster-ensure-schedule:
	@kubectl -n research exec deploy/dagster-daemon -- \
	  dagster schedule list -m bifrost_research.orchestration.definitions 2>/dev/null \
	  | awk '/Schedule:/{print $$2}' \
	  | while read -r name; do \
	      echo "start $$name"; \
	      kubectl -n research exec deploy/dagster-daemon -- \
	        dagster schedule start "$$name" -m bifrost_research.orchestration.definitions || true; \
	    done

verify-husbandry-schedulers:
	bash scripts/verify_husbandry_schedulers.sh

lint:
	ruff check src/ tests/ || true
	@if [ -d "$(DBT_DIR)/models" ]; then \
		cd $(DBT_DIR) && sqlfluff lint models/ || true; \
	fi

test:
	pytest -q

run-api:
	python scripts/run_api.py

run-mcp:
	python scripts/run_mcp.py

test-mcp:
	pytest -q tests/mcp/

smoke-agents-sdk:
	.venv/bin/python scripts/smoke_agents_sdk.py

# Patch OPENAI_API_KEY into K8s secret bifrost-research-secrets (namespace research).
# Usage: export OPENAI_API_KEY='sk-...' && make sync-openai-secret
sync-openai-secret:
	bash scripts/sync_openai_secret.sh

db-init-analytics:
	python -c "from bifrost_research.db.conn import connect; from bifrost_research.schema.ddl import apply_market_analytics_ddl, ensure_month_partitions; c=connect(); apply_market_analytics_ddl(c); ensure_month_partitions(c); c.close(); print('market_analytics DDL applied')"

db-init-research:
	python -c "from bifrost_research.db.conn import connect; from bifrost_research.schema.ddl import apply_all_ddl, ensure_month_partitions; c=connect(); apply_all_ddl(c); ensure_month_partitions(c); c.close(); print('features Feature Store DDL applied')"

db-migrate-6-4:
	python scripts/apply_wave_6_4_migration.py

db-migrate-6-6:
	python scripts/apply_wave_6_6_migration.py

# CNPG postgres superuser — run via kubectl exec (see scripts/wave_6_4_superuser_fixup.sql)
db-migrate-6-4-superuser:
	@echo "Apply on CNPG primary: cat scripts/wave_6_4_superuser_fixup.sql | kubectl -n data exec -i \$$(kubectl get cluster bifrost-postgres -n data -o jsonpath='{.status.currentPrimary}') -- psql -U postgres -d bifrost_golden_source -v ON_ERROR_STOP=1"

# Owner decision A — file ingest (see docs/EVENT_RADAR_INGEST.md)
# EVENT_RADAR_INPUT_DIR defaults to Research-workspace offline input when unset locally.
EVENT_RADAR_INPUT_DIR ?= $(HOME)/Desktop/stocks/Research-workspace/事件雷达工作流/input
event-radar-ingest:
	EVENT_RADAR_INPUT_DIR="$(EVENT_RADAR_INPUT_DIR)" python -m bifrost_research.scheduler.event_radar

IMAGE_VERSION ?= 0.50.6
REGISTRY ?= 192.168.10.73:30500

build-image:
	docker build --platform linux/amd64 --target base -t bifrost-research:$(IMAGE_VERSION) -f Dockerfile .
	docker build --platform linux/amd64 --target base -t bifrost-research:latest -f Dockerfile .
	docker tag bifrost-research:$(IMAGE_VERSION) $(REGISTRY)/bifrost-research:$(IMAGE_VERSION)
	docker tag bifrost-research:latest $(REGISTRY)/bifrost-research:latest
	docker push $(REGISTRY)/bifrost-research:$(IMAGE_VERSION)
	docker push $(REGISTRY)/bifrost-research:latest

build-image-dagster:
	docker build --platform linux/amd64 --target orchestration -t bifrost-research:$(IMAGE_VERSION)-dagster -f Dockerfile .
	docker tag bifrost-research:$(IMAGE_VERSION)-dagster $(REGISTRY)/bifrost-research:$(IMAGE_VERSION)-dagster
	docker push $(REGISTRY)/bifrost-research:$(IMAGE_VERSION)-dagster

db-init-ops-dagster:
	@echo "Apply scripts/ops_dagster_schema.sql on Golden Source (ops_dagster schema for Dagster instance storage)"
	@echo "Example: psql \$$ANALYTICS_PG_* -f scripts/ops_dagster_schema.sql"
