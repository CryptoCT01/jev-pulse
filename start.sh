#!/usr/bin/env bash
# Jev Pulse — start the :8790 desk in tmux so it survives terminal/session exit.
#
#   ./start.sh          start everything (idempotent — safe to run repeatedly)
#   ./start.sh status   show what's running
#   ./start.sh stop     stop everything
#
# Why tmux: launched as plain background processes these die when the launching
# session ends. tmux keeps them alive independently, like hbotdash/crossfire.

set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="/usr/local/bin/python3"   # never /usr/bin/python3 (3.9, no `dict | None`)

cmd="${1:-start}"

health() { curl -sf -m 2 http://127.0.0.1:8790/api/health >/dev/null 2>&1; }

status() {
  echo "--- tmux sessions ---"; tmux ls 2>/dev/null || echo "(none)"
  echo "--- listeners ---"; lsof -nP -iTCP:8790 -sTCP:LISTEN | tail -n +2 || echo "8790 FREE"
  echo "--- processes ---"
  echo "  companion: $(pgrep -f run_companion_loop | wc -l | tr -d ' ')"
  echo "  keepalive: $(pgrep -f keep_dash_alive | wc -l | tr -d ' ')"
  echo "--- health ---"; health && echo "  OK $(curl -sf -m 2 http://127.0.0.1:8790/api/health)" || echo "  DOWN"
}

stop() {
  tmux kill-session -t jevpulse 2>/dev/null && echo "killed tmux jevpulse" || echo "no jevpulse session"
  pkill -f 'scripts/dash_server.py' 2>/dev/null || true
  pkill -f run_companion_loop 2>/dev/null || true
  pkill -f keep_dash_alive 2>/dev/null || true
  sleep 1
  lsof -nP -iTCP:8790 -sTCP:LISTEN >/dev/null 2>&1 && echo "8790 STILL BOUND" || echo "8790 free"
}

start() {
  if health; then
    echo "already up: $(curl -sf -m 2 http://127.0.0.1:8790/api/health)"
    status; return 0
  fi

  # one tmux session, three panes — survives this terminal closing
  tmux kill-session -t jevpulse 2>/dev/null || true
  tmux new-session -d -s jevpulse -c "$PWD" \; \
    send-keys "exec $PY scripts/dash_server.py >> logs/dash_server.log 2>&1" C-m \; \
    split-window -h -c "$PWD" \; \
    send-keys "exec bash scripts/keep_dash_alive.sh >> logs/dash_keepalive.log 2>&1" C-m \; \
    split-window -v -c "$PWD" \; \
    send-keys "exec ./scripts/run_companion_loop.sh 5" C-m \; \
    select-layout tiled

  echo "waiting for :8790 ..."
  for i in $(seq 1 20); do
    if health; then echo "UP after ${i}s"; status; return 0; fi
    sleep 1
  done

  echo "did not come up — last log lines:"
  tail -n 15 logs/dash_server.log 2>/dev/null || true
  status
  return 1
}

case "$cmd" in
  start)  start ;;
  status) status ;;
  stop)   stop ;;
  *)      echo "usage: $0 [start|status|stop]"; exit 2 ;;
esac
