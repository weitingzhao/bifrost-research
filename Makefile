.PHONY: install-dev install-orchestration dbt-deps dbt-run dbt-test dbt-docs dbt-clean dbt-parse lint test build-image build-image-dagster run-api db-init-analytics db-init-research dagster-dev dagster-defs event-radar-ingest

DBT_DIR := src/bifrost_research/dbt
DBT_PROFILES := $(DBT_DIR)

install-dev:
	pip install -e ".[dev]"

install-orchestration:
	pip install -e ".[dev,orchestration]"

dbt-deps:
	cd $(DBT_DIR) && dbt deps --profiles-dir .

dbt-run:
	cd $(DBT_DIR) && dbt run --profiles-dir .

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

lint:
	ruff check src/ tests/ || true
	@if [ -d "$(DBT_DIR)/models" ]; then \
		cd $(DBT_DIR) && sqlfluff lint models/ || true; \
	fi

test:
	pytest -q

run-api:
	python scripts/run_api.py

db-init-analytics:
	python -c "from bifrost_research.db.conn import connect; from bifrost_research.schema.ddl import apply_market_analytics_ddl, ensure_month_partitions; c=connect(); apply_market_analytics_ddl(c); ensure_month_partitions(c); c.close(); print('market_analytics DDL applied')"

db-init-research:
	python -c "from bifrost_research.db.conn import connect; from bifrost_research.schema.ddl import apply_all_ddl, ensure_month_partitions; c=connect(); apply_all_ddl(c); ensure_month_partitions(c); c.close(); print('market_analytics + research DDL applied')"

# Owner decision A — file ingest (see docs/EVENT_RADAR_INGEST.md)
# EVENT_RADAR_INPUT_DIR defaults to Research-workspace offline input when unset locally.
EVENT_RADAR_INPUT_DIR ?= $(HOME)/Desktop/stocks/Research-workspace/事件雷达工作流/input
event-radar-ingest:
	EVENT_RADAR_INPUT_DIR="$(EVENT_RADAR_INPUT_DIR)" python -m bifrost_research.scheduler.event_radar

build-image:
	docker build --platform linux/amd64 --target base -t bifrost-research:0.5.7 -f Dockerfile .
	docker build --platform linux/amd64 --target base -t bifrost-research:latest -f Dockerfile .
	docker tag bifrost-research:0.5.7 192.168.10.73:30500/bifrost-research:0.5.7
	docker tag bifrost-research:latest 192.168.10.73:30500/bifrost-research:latest
	docker push 192.168.10.73:30500/bifrost-research:0.5.7
	docker push 192.168.10.73:30500/bifrost-research:latest

build-image-dagster:
	docker build --platform linux/amd64 --target orchestration -t bifrost-research:0.5.7-dagster -f Dockerfile .
