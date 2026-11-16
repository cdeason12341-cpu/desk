#!/usr/bin/env bash
# Start the desk if it isn't running (idempotent; used by desk-health and after a box restart).
# Also ensures the out-of-process supervisor is running.
cd "$(dirname "$0")"
mkdir -p logs
# Box restores can drop the gitignored .venv: rebuild it from requirements.txt (DECISION 50).
# Fast path when the venv is healthy: one tiny import check. flock stops two callers building at once.
ensure_venv() {
  if [ -x .venv/bin/python ] && .venv/bin/python -c "import httpx, websockets, cryptography" 2>/dev/null; then
    return 0
  fi
  (
    flock -w 600 9 || { echo "venv lock timeout" >&2; exit 1; }
    if [ -x .venv/bin/python ] && .venv/bin/python -c "import httpx, websockets, cryptography" 2>/dev/null; then exit 0; fi
    echo "$(date -u +%FT%TZ) rebuilding .venv from requirements.txt" >> logs/desk.out
    PY="$(command -v python3.13 || command -v python3)"
    rm -rf .venv
    "$PY" -m venv .venv >> logs/desk.out 2>&1 &&
      .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt >> logs/desk.out 2>&1
  ) 9>logs/venv.lock
}
if ! ensure_venv; then
  echo "venv rebuild failed (see logs/desk.out)" >&2
  exit 1
fi
if [ -f logs/desk.pid ] && kill -0 "$(cat logs/desk.pid)" 2>/dev/null; then
  echo "running pid $(cat logs/desk.pid)"
else
  nohup .venv/bin/python main.py >> logs/desk.out 2>&1 &
  echo $! > logs/desk.pid
  echo "started pid $!"
fi
if [ -f logs/supervise.pid ] && kill -0 "$(cat logs/supervise.pid)" 2>/dev/null; then
  echo "supervise running pid $(cat logs/supervise.pid)"
else
  nohup .venv/bin/python supervise_desk.py >> logs/supervise.out 2>&1 &
  echo $! > logs/supervise.pid
  echo "started supervise pid $!"
fi
