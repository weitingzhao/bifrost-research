#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
# .env may set PORT=8795 for research-api — force MCP port after source.
export HOST="${MCP_HOST:-0.0.0.0}"
export PORT="${MCP_PORT:-8796}"
exec .venv/bin/python scripts/run_mcp.py
