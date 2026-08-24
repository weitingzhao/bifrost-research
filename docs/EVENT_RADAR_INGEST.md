# Event Radar news ingest — Owner decision A

**Operate Queue**: `research-radar-news-source` (`1b9258ea-ff4d-4bad-b4ea-9cd2ff2fb382`)  
**Decision**: **A** — Research-workspace input directory → Cron → Event Radar pipeline → `features.event_signal_radar_daily`  
**Constraints**: D10 BLOCKED · D13 OLAP-only (no Trade DB / no live trading)

## Path convention

| Surface | Path |
|---------|------|
| Offline workspace (Owner drop zone) | `Research-workspace/事件雷达工作流/input/` |
| Local Cron / CLI default override | `EVENT_RADAR_INPUT_DIR=<abs path to input/>` |
| K8s CronJob mount | `/data/event-radar/input` (+ `/data/event-radar/archive`) |
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

## K8s apply

```bash
# From Mac Apple Silicon — must target cluster arch:
docker build --platform linux/amd64 --target base \
  -t 192.168.10.73:30500/bifrost-research:0.5.2 -f Dockerfile .
docker push 192.168.10.73:30500/bifrost-research:0.5.2

kubectl apply -f k8s/engines/cronjob-event-radar.yaml
# Optional: copy Owner files into the PVC (or sync from Research-workspace)
kubectl -n research create job --from=cronjob/research-engines-event-radar event-radar-manual-$(date +%s)
```

Mac node hostPath alternative is documented as a comment in the CronJob YAML.

## Empty state retirement

Trade FE `EventRadarPage` shows the events table when API returns rows.
"News source not configured" is replaced by a "No events yet" empty state that
points at the Research-workspace input path once this ingest Cron exists.
