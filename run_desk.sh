#!/usr/bin/env bash
# Start the desk if it isn't running (idempotent; used by desk-health and after a box restart).
cd "$(dirname "$0")"
if [ -f logs/desk.pid ] && kill -0 "$(cat logs/desk.pid)" 2>/dev/null; then echo "running pid $(cat logs/desk.pid)"; exit 0; fi
mkdir -p logs
nohup .venv/bin/python main.py >> logs/desk.out 2>&1 &
echo $! > logs/desk.pid
echo "started pid $!"
