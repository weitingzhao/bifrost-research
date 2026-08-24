#!/usr/bin/env bash
set -euo pipefail

cd /app

echo "=== bifrost-research dbt run ==="
echo "Started at: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "Target: ${DBT_TARGET:-dev}"

# Install dbt packages if not present
if [ ! -d "dbt_packages" ]; then
    echo "Installing dbt packages..."
    dbt deps --profiles-dir .
fi

# Run dbt models
echo "Running dbt..."
dbt run --target "${DBT_TARGET:-dev}" --profiles-dir .

# Run tests (non-blocking so Elementary still collects artifacts)
echo "Running dbt tests..."
dbt test --target "${DBT_TARGET:-dev}" --profiles-dir . || echo "Some tests failed (non-blocking)"

# Elementary metadata collection (artifacts → ops_dbt)
echo "Running Elementary models..."
dbt run --select elementary --target "${DBT_TARGET:-dev}" --profiles-dir .

# Generate Elementary HTML report (Wave 2: /report PVC; local fallback: target/)
REPORT_PATH="${ELEMENTARY_REPORT_PATH:-/report/elementary_report.html}"
REPORT_DIR="$(dirname "$REPORT_PATH")"
if [ ! -d "$REPORT_DIR" ]; then
    REPORT_PATH="target/elementary_report.html"
    REPORT_DIR="$(dirname "$REPORT_PATH")"
    mkdir -p "$REPORT_DIR"
fi

if command -v edr >/dev/null 2>&1; then
    echo "Generating Elementary report → $REPORT_PATH"
    # edr uses the dedicated 'elementary' profile in profiles.yml
    edr report \
        --profiles-dir . \
        -t "${DBT_TARGET:-dev}" \
        --file-path "$REPORT_PATH" \
        || echo "Elementary report generation failed (non-blocking)"
    echo "Report path: $REPORT_PATH"
else
    echo "edr CLI not installed — skipping report generation"
fi

echo "=== Completed at: $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="
