#!/usr/bin/env bash
cd /Users/cryptot/Desktop/jev-pulse || exit 1
mkdir -p logs
while true; do
  if ! curl -sf -m 1 http://127.0.0.1:8790/api/health >/dev/null 2>&1; then
    pkill -f 'scripts/dash_server.py' 2>/dev/null || true
    sleep 0.3
    ./.venv/bin/python scripts/dash_server.py >> logs/dash_server.log 2>&1 &
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') restarted dash pid=$!" >> logs/dash_keepalive.log
    sleep 1
  fi
  sleep 2
done
