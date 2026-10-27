#!/usr/bin/env bash
# paper_supervisor.sh — keep the paper trader alive all day on the operator
# machine. Restarts on crash with backoff, timestamps everything, and stops
# cleanly when the kill file exists.
#
# Usage:  scripts/paper_supervisor.sh [start|stop|status]
# E.g.:   ./scripts/paper_supervisor.sh start
#
# KILL switch:  touch KILL   (supervisor exits; open orders are cancelled
#               by main.py's signal/kill-file handling on the next check)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/logs"
KILL_FILE="$ROOT/KILL"
STATE_FILE="$ROOT/state/paper_supervisor.pid"
mkdir -p "$LOG_DIR" "$ROOT/state"

now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

record_run() {  # record start/stop of soak runs -> state/soak_log.json
  "$ROOT/.venv/bin/python" - "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
action, ts = sys.argv[1], sys.argv[2]
p = Path("state/soak_log.json")
runs = json.loads(p.read_text()) if p.exists() else []
if action == "start" and runs and runs[-1].get("ended_at") is None:
    pass                       # avoid double-start after a supervisor crash
else:
    runs.append({"started_at": ts if action == "start" else None,
                 "ended_at": ts if action == "stop" else None})
    if action == "stop":
        for r in runs:
            if r.get("ended_at") is None and r.get("started_at"):
                r["ended_at"] = ts
p.parent.mkdir(exist_ok=True)
p.write_text(json.dumps(runs, indent=1))
PY
}

start() {
  if [ -f "$STATE_FILE" ] && kill -0 "$(cat "$STATE_FILE")" 2>/dev/null; then
    echo "supervisor already running (pid $(cat "$STATE_FILE"))"; exit 0
  fi
  if [ -f "$KILL_FILE" ]; then
    # kill switch is operator-only: never auto-remove it
    echo "KILL file present — refusing to start. Operator must 'rm KILL'." >&2
    exit 1
  fi
  echo $$ > "$STATE_FILE"
  echo "$(now) supervisor started (pid $$)" >> "$LOG_DIR/supervisor.log"
  record_run start "$(now)"
  backoff=5
  while true; do
    if [ -f "$KILL_FILE" ]; then
      echo "$(now) KILL file present — stopping" >> "$LOG_DIR/supervisor.log"
      break
    fi
    echo $$ > "$STATE_FILE"     # refresh so stop() always finds us
    echo $$ > "$LOG_DIR/child.pid" 2>/dev/null || true
    echo "$(now) launching paper run" >> "$LOG_DIR/supervisor.log"
    # paper_session journals daily.csv/trades.csv/paper_state.json under
    # paper/<strategy>/ (what the dashboard reads) — unlike `python -m main`.
    # Each run is a bounded window (default 4h = 6 equity snapshots/day);
    # the supervisor relaunches it, backoff resets on clean exits.
    "$ROOT/.venv/bin/python" "$ROOT/src/tools/paper_session.py" \
      --strategy credit_spread \
      --minutes "${PAPER_SESSION_MINUTES:-240}" \
      >> "$LOG_DIR/paper.out" 2>&1 &
    child=$!
    echo "$child" > "$ROOT/state/paper_child.pid"
    wait "$child"
    rc=$?
    if [ "$rc" -eq 0 ]; then backoff=5; fi   # clean expiry: restart immediately
    if [ -f "$KILL_FILE" ]; then break; fi
    echo "$(now) paper run exited rc=$rc; restarting in ${backoff}s" \
      >> "$LOG_DIR/supervisor.log"
    sleep "$backoff"
    backoff=$(( backoff * 2 )); [ "$backoff" -gt 300 ] && backoff=300
    # soak accounting: a crash-restart is still one continuous soak day
    if ! pgrep -f "paper_supervisor" >/dev/null; then break; fi
  done
  record_run stop "$(now)"
  echo "$(now) supervisor stopped" >> "$LOG_DIR/supervisor.log"
  rm -f "$STATE_FILE"
}

stop() {
  created_kill=0
  if [ ! -f "$KILL_FILE" ]; then
    touch "$KILL_FILE"          # only we may auto-clear it on exit
    created_kill=1
  fi
  if [ -f "$STATE_FILE" ]; then
    pid=$(cat "$STATE_FILE")
    # SIGTERM lets the supervisor's loop see KILL and stop the child cleanly
    kill "$pid" 2>/dev/null
    wait_loops=0
    while kill -0 "$pid" 2>/dev/null && [ $wait_loops -lt 25 ]; do
      sleep 1
      wait_loops=$((wait_loops+1))
    done
    kill -9 "$pid" 2>/dev/null
  fi
  # Kill any tracked child main process...
  CHILD_FILE="$ROOT/state/paper_child.pid"
  if [ -f "$CHILD_FILE" ]; then
    child=$(cat "$CHILD_FILE" 2>/dev/null)
    if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
      kill "$child" 2>/dev/null && echo "main (pid $child) terminated"
    fi
    rm -f "$CHILD_FILE"
  fi
  # ...and sweep any other orphan paper session (paranoia stay single-instance)
  pkill -f "paper_session.py.*credit_spread" \
    2>/dev/null || true
  rm -f "$STATE_FILE" "$KILL_FILE" 2>/dev/null
  if [ "$created_kill" = 1 ]; then
    echo "stopped; KILL file (created by this stop) cleared for next start"
  else
    echo "stopped (operator KILL stays in place — remove it yourself)"
  fi
}

status() {
  if [ -f "$STATE_FILE" ] && kill -0 "$(cat "$STATE_FILE")" 2>/dev/null; then
    echo "RUNNING pid=$(cat "$STATE_FILE")"
  else
    echo "NOT RUNNING"
  fi
  [ -f "$KILL_FILE" ] && echo "KILL file PRESENT (remove to allow trading)"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/paper_gate.py" 2>/dev/null || true
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 [start|stop|status]"; exit 2 ;;
esac
