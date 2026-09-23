#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
set -a; source "$ROOT/.env"; set +a
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "OPENROUTER_API_KEY is empty in .env — add it then re-run."
  exit 1
fi
INTERVAL="${1:-1}"
PY="${PYTHON_BIN:-/opt/homebrew/bin/python3}"
echo "Jev Pulse companion every ${INTERVAL}s py=$PY → logs/ (Ctrl-C to stop)"
mkdir -p logs
while true; do
  "$PY" scripts/paper_tick.py >> logs/companion_loop.log 2>&1 || echo "tick failed at $(date -u +%H:%M:%S)" >> logs/companion_loop.log
  sleep "$INTERVAL"
done
