FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir dbt-core dbt-postgres 'elementary-data[postgres]'

# Build context: bifrost-research repo root
COPY src/bifrost_research/dbt/dbt_project.yml src/bifrost_research/dbt/profiles.yml src/bifrost_research/dbt/packages.yml ./
COPY src/bifrost_research/dbt/models/ models/
COPY src/bifrost_research/dbt/macros/ macros/
COPY src/bifrost_research/dbt/tests/ tests/
COPY src/bifrost_research/dbt/scripts/ scripts/

RUN chmod +x scripts/run_dbt.sh

RUN dbt deps --profiles-dir .

ENTRYPOINT ["scripts/run_dbt.sh"]
