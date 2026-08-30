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

# Prefer baking a pre-parsed dbt manifest when present in build context.
# Local: `make dbt-parse` then `make build-image-dagster`.
ENV DAGSTER_HOME=/opt/dagster/home

CMD ["dagster-webserver", "-h", "0.0.0.0", "-p", "3000", "-w", "/app/workspace.yaml"]
