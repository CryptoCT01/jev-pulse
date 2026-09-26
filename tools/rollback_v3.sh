#!/usr/bin/env bash
# Roll Jev Pulse back from v3 HF to the pre-v3 (v2 rules + dash-v2 candles) state.
# Usage: tools/rollback_v3.sh <backups/strategy-v2-YYYYmmdd-HHMM> <logs/archive/run1-fees-YYYYmmdd-HHMM>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
B="${1:?backup dir}"; A="${2:?archive dir}"
echo "1) stop the v3 supervisor + engine by PID (never pkill -f)"
for pat in 'run_companion_loop.sh' 'scripts/hf_engine.py'; do
  for pid in $(ps -axo pid=,command= | awk -v p="$pat" 'index($0,p) && !index($0,"awk") && !index($0,"rollback_v3") {print $1}'); do
    echo "   kill $pid ($(ps -p "$pid" -o command= | cut -c1-60))"; kill "$pid" 2>/dev/null || true
  done
done
sleep 2
echo "2) park the v3 run"
V3A="logs/archive/v3-hf-$(date +%Y%m%d-%H%M)"; mkdir -p "$V3A"
for f in logs/paper_ticks.jsonl logs/paper_fills.jsonl .state/paper_account.json .state/hf_gate.json .state/jev_stance.json; do
  [[ -f "$f" ]] && mv "$f" "$V3A/"
done
echo "3) restore code + dash + run1 state"
cp -p "$B/scripts/run_companion_loop.sh" scripts/
cp -p "$B/scripts/dash_server.py" scripts/ && cp -p "$B/dash/index.html" dash/
cp -p "$A/paper_ticks.jsonl" logs/ 2>/dev/null || echo "   (run1 ticks stay archived; copy back by hand if wanted)"
cp -p "$A/paper_account.json" .state/ && cp -p "$A/jev_stance.json" .state/ 2>/dev/null || true
echo "4) restart: dash server (keep-alive relaunches it) and the old 5 s loop"
echo "   run:  nohup scripts/run_companion_loop.sh 5 >/dev/null 2>&1 &   and kill the dash_server PID once"
echo "done. v3 files (hf_*.py) are left in scripts/ unused; v3 run parked in $V3A"
