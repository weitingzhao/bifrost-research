# Event Radar news ingest — Owner decision A

**Operate Queue**: `research-radar-news-source` (`1b9258ea-ff4d-4bad-b4ea-9cd2ff2fb382`)  
**Decision**: **A** — Research-workspace input directory → Cron → Event Radar pipeline → `features.event_signal_radar_daily`  
**Constraints**: D10 BLOCKED · D13 OLAP-only (no Trade DB / no live trading)

## Path convention

| Surface | Path |
|---------|------|
| Offline workspace (Owner drop zone) | `Research-workspace/事件雷达工作流/input/` |
| Local Cron / CLI default override | `EVENT_RADAR_INPUT_DIR=<abs path to input/>` |
| Default inside a container | `/data/event-radar/input` (+ `/data/event-radar/archive`); nothing mounts it in the cluster |
| Processed files | `EVENT_RADAR_ARCHIVE_DIR` (default: sibling `archive/` of input) |

Supported suffixes: `.txt` `.md` `.json` `.csv` `.eml`  
Skipped: `放这里.md`, `README.md`, dotfiles.

## Flow

```
Owner drops raw news into input/
        │
        ▼
python -m bifrost_research.scheduler.event_radar
        │  read files → run_pipeline → upsert features.event_signal_radar_daily
        ▼
archive/YYYYMMDDTHHMMSSZ_<filename>   (EVENT_RADAR_ARCHIVE=1)
        │
        ▼
GET /research/event-radar/events  → Trade FE Event Radar table
```

## Local smoke

```bash
# From bifrost-research
export EVENT_RADAR_INPUT_DIR="$HOME/Desktop/stocks/Research-workspace/事件雷达工作流/input"
# or a temp dir:
mkdir -p /tmp/event-radar-input
printf '%s\n' '- Fed announced rate pause on 2026-08-21; $SPY rallies.' \
  > /tmp/event-radar-input/sample.txt
EVENT_RADAR_INPUT_DIR=/tmp/event-radar-input EVENT_RADAR_ARCHIVE=0 \
  python -m bifrost_research.scheduler.event_radar

# Dry-run (no DB):
python -m bifrost_research.scheduler.event_radar \
  --input-dir /tmp/event-radar-input --dry-run

# Unit tests (file → pipeline, no live DB):
pytest -q tests/engines/test_event_radar_ingest.py
```

## In the cluster (Dagster)

There is no event-radar CronJob any more: `research-engines-event-radar` and its
`event-radar-input-pvc` were suspended Job templates for platform's trigger
route, and both went with it (TD-190, 2026-10). The cluster side is Dagster's
`research_event_radar_job` (SEC 8-K filings, below), on
`research_event_radar_schedule` every 30 minutes. To run it once by hand:

```bash
kubectl -n research exec deploy/dagster-daemon -- \
  dagster job launch -w /opt/dagster/workspace.yaml -j research_event_radar_job
```

The Owner's drop-zone files are drained on the Mac (Local watcher, below); the
cluster has no mount for them.

## Empty state retirement

Trade FE `EventRadarPage` shows the events table when API returns rows.
"News source not configured" is replaced by a "No events yet" empty state that
points at the Research-workspace input path.

## SEC 8-K filings — in Dagster (TD-100, 2026-10)

`research_event_radar_schedule` (every 30 minutes, every day) materializes
`engines/event_radar_cron`, which reads `raw_market.sec_8k_filing` (+ the
vendor's `sec_8k_disclosure` category) for the last 7 days, skips filings whose
line is already in `features.event_signal_radar_daily`, and upserts the rest
(`engines/event_radar/sec_source.py`). There is no watermark file: a filing is
"new" when its head (`<filing_date> <symbol> filed an 8-K (items …)`) is not in
the table as many times as filings share it, so a missed tick is caught up by
the next one and a re-run writes nothing. A database failure fails the run, and
`bifrost_run_failure_alert` reports it. Rows keep the `ws:sec-8k-<UTC stamp>`
source and the Central-time `collected_at` the Mac loop used.

Manual run (inside a research pod): `python -m bifrost_research.engines.event_radar.sec_source`.

Liveness: research-api `/metrics` exports the newest filing's `fetched_at` and
the newest SEC radar row's `computed_at`; `BifrostEventRadarSecBacklog` and
`BifrostEventRadarStale` (bifrost-trade-infra `k8s/monitoring`) fire when the
second falls behind the first or goes four days without a row.

Until 2026-10 this ran on the Owner's Mac (`scripts/event_radar_sec_source.py`
in the bdev watcher, watermark `Research-workspace/事件雷达工作流/.sec-8k-watermark.json`);
both are gone.

## Local watcher (optional — Owner drop-zone files only)

The Owner's drop zone is `Research-workspace/事件雷达工作流/input/` on the Mac and
the cluster has no mount for it, so files dropped there are drained on the Mac,
under bdev (a launchd job cannot reach the LAN database — macOS grants local
network per executable and launchd does not inherit it):

```bash
bdev start event-radar-watch     # scripts/event_radar_watch.sh
```

The watcher checks the input directory every 10 minutes and runs the ingest
only when supported files are present. It re-reads `.env` each time and exits
non-zero after three consecutive failed ingests, so bdev shows it. With no files
to drop it need not run. Wiring the PVC into the Dagster daemon (mount +
`EVENT_RADAR_INPUT_DIR`) plus a Mac→PVC sync remains the cluster-native
alternative if the drop zone ever moves off this machine.
