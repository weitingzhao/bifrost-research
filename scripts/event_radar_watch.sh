#!/usr/bin/env bash
# Event Radar local watcher — drains Research-workspace/事件雷达工作流/input/
# every 10 minutes when it holds supported files (.txt .md .json .csv .eml).
#
# Why this exists on the Mac rather than only in the cluster: the Dagster
# schedule that owns the event-radar slot runs in a pod with no mount for the
# input PVC, and the Owner's drop zone is this workspace directory on this
# machine. A launchd job cannot reach the LAN database (macOS grants local
# network per executable and launchd does not inherit it), so the watcher runs
# under the bdev tmux session, which was created with that permission.
#
# D10 BLOCKED / D13 OLAP-only — the ingest writes research's own schema.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; source .env; set +a

INPUT_DIR="${EVENT_RADAR_INPUT_DIR:-$HOME/Desktop/stocks/Research-workspace/事件雷达工作流/input}"
export EVENT_RADAR_INPUT_DIR="$INPUT_DIR"

echo "event-radar watch: draining $INPUT_DIR every 600s"
while true; do
  # Pull any new SEC 8-K filings into input/ first (financial-text source;
  # reads raw_market, which D13 allows research to read).
  .venv/bin/python scripts/event_radar_sec_source.py || echo "$(date '+%F %T') sec source failed (will retry next tick)"
  # Anything supported and not a placeholder?
  if find "$INPUT_DIR" -maxdepth 1 -type f \
       \( -name '*.txt' -o -name '*.md' -o -name '*.json' -o -name '*.csv' -o -name '*.eml' \) \
       ! -name '放这里.md' ! -name 'README.md' ! -name 'readme.md' | grep -q .; then
    echo "$(date '+%F %T') files present — running ingest"
    .venv/bin/python -m bifrost_research.scheduler.event_radar || echo "$(date '+%F %T') ingest failed (will retry next tick)"
  fi
  sleep 600
done
