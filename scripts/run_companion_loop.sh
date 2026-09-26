#!/usr/bin/env bash
# Jev Pulse v3 companion: supervises ONE persistent HF paper engine (scripts/hf_engine.py).
# Cadence lives in scripts/hf_params.json (2.5 s). The optional first arg is kept for
# backwards compatibility with `run_companion_loop.sh 5` and is only the restart back-off.
# PAPER ONLY: the engine has no order code and never sends orders to Bitget.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
set -a; source "$ROOT/.env"; set +a
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "OPENROUTER_API_KEY is empty in .env — add it then re-run."
  exit 1
fi
BACKOFF="${1:-5}"
PY="${PYTHON_BIN:-$ROOT/.venv/bin/python}"   # venv has `websockets`
mkdir -p logs
echo "$(date '+%Y-%m-%dT%H:%M:%S%z') Jev Pulse v3 HF engine supervisor py=$PY backoff=${BACKOFF}s" >> logs/companion_loop.log
trap 'kill "${CHILD:-}" 2>/dev/null; exit 0' TERM INT
while true; do
  "$PY" -u scripts/hf_engine.py >> logs/companion_loop.log 2>&1 &
  CHILD=$!
  wait "$CHILD" || true
  echo "tick failed at $(date -u +%H:%M:%S): engine exited, restarting in ${BACKOFF}s" >> logs/companion_loop.log
  sleep "$BACKOFF"
done
