#!/usr/bin/env bash
# Event Radar local watcher — OPTIONAL. Drains the Owner's drop zone,
# Research-workspace/事件雷达工作流/input/, every 10 minutes when it holds
# supported files (.txt .md .json .csv .eml).
#
# SEC 8-K filings no longer come through here: since TD-100 (2026-10) the
# cluster's research_event_radar_schedule reads raw_market.sec_8k_filing and
# writes the radar rows itself. This loop exists only because the drop zone is
# a directory on this Mac and the cluster has no mount for it; with no files to
# drop, it does not need to run.
#
# A launchd job cannot reach the LAN database (macOS grants local network per
# executable and launchd does not inherit it), so the watcher runs under the
# bdev tmux session, which was created with that permission.
#
# .env is re-read every tick, so a rotated password is picked up without a
# restart. After MAX_FAILURES consecutive failed ingests the script exits
# non-zero, so bdev's restart policy and `bdev status` surface it instead of a
# log line nobody reads (it once failed 157 ticks in a row unnoticed).
#
# D10 BLOCKED / D13 OLAP-only — the ingest writes research's own schema.
set -uo pipefail

cd "$(dirname "$0")/.."

INPUT_DIR="${EVENT_RADAR_INPUT_DIR:-$HOME/Desktop/stocks/Research-workspace/事件雷达工作流/input}"
export EVENT_RADAR_INPUT_DIR="$INPUT_DIR"
INTERVAL="${EVENT_RADAR_WATCH_INTERVAL:-600}"
MAX_FAILURES="${EVENT_RADAR_WATCH_MAX_FAILURES:-3}"

has_files() {
  find "$INPUT_DIR" -maxdepth 1 -type f \
    \( -name '*.txt' -o -name '*.md' -o -name '*.json' -o -name '*.csv' -o -name '*.eml' \) \
    ! -name '放这里.md' ! -name 'README.md' ! -name 'readme.md' | grep -q .
}

echo "event-radar watch: draining $INPUT_DIR every ${INTERVAL}s (files only; SEC runs in Dagster)"
failures=0
while true; do
  if has_files; then
    echo "$(date '+%F %T') files present — running ingest"
    if ( set -a; source .env; set +a; .venv/bin/python -m bifrost_research.scheduler.event_radar ); then
      failures=0
    else
      failures=$((failures + 1))
      echo "$(date '+%F %T') ingest failed (${failures}/${MAX_FAILURES} in a row)"
      if [ "$failures" -ge "$MAX_FAILURES" ]; then
        echo "$(date '+%F %T') giving up after ${failures} consecutive failures"
        exit 1
      fi
    fi
  fi
  sleep "$INTERVAL"
done
