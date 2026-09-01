# bifrost-research — Research Engine image
#
# Targets:
#   base (default)     — Research API + CronJob engines/dbt runners
#   orchestration      — base + Dagster ([orchestration] extra)
#
# Build:
#   docker build --target base -t bifrost-research:0.49.0 -f Dockerfile .
#   docker build --target orchestration -t bifrost-research:0.49.0-dagster -f Dockerfile .
#
# Optional: run `make dbt-parse` before build so dbt/target/manifest.json is
# available for dagster-dbt (gitignored; absent → dbt assets skipped at runtime).

# --- base ---
FROM python:3.11-slim-bookworm AS base

WORKDIR /app
RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
COPY workspace.yaml ./

RUN pip install --no-cache-dir ".[copilot]"

ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8795

# Default: Research API. CronJobs override with:
#   args: ["python", "-m", "bifrost_research.scheduler.volatility", ...]
CMD ["python", "scripts/run_api.py"]

# --- orchestration (Dagster) ---
FROM base AS orchestration

RUN pip install --no-cache-dir ".[orchestration]"

# dagster-dbt only produces assets when dbt/target/manifest.json is in the image.
# `target/` is gitignored and builds run in-cluster from the Gitea mirror, so a
# manifest parsed on a laptop never reaches the image — and `load_dbt_assets()`
# returning empty is silent: the trading-day job still succeeds, sepa_projection
# still materializes, and it copies a mart nothing has refreshed. That is how
# dw_stock.mart_sepa_* sat frozen while every dashboard read green.
#
# Parse during the build instead. No database is touched: `deps` fetches the
# packages pinned in package-lock.yml and `parse` only renders the project.
# Parse inside the *installed* package: orchestration/paths.py resolves the
# project from `bifrost_research.__file__`, which is site-packages here and the
# source tree only under an editable install — which is why this worked on a
# laptop and never once in a container.
RUN DBT_DIR="$(python -c 'import bifrost_research, os; print(os.path.join(os.path.dirname(bifrost_research.__file__), "dbt"))')" \
    && cd "$DBT_DIR" \
    && dbt deps \
    && dbt parse --profiles-dir . --no-partial-parse \
    && test -s target/manifest.json \
    && python -c "from bifrost_research.orchestration.dbt_assets import load_dbt_assets; assert load_dbt_assets(), 'dbt assets still empty'"

ENV DAGSTER_HOME=/opt/dagster/home

CMD ["dagster-webserver", "-h", "0.0.0.0", "-p", "3000", "-w", "/app/workspace.yaml"]
