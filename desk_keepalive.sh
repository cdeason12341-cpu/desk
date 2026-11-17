#!/usr/bin/env bash
# systemd entrypoint on the droplet (desk.service). Mirrors the box's desk-health routine but every 30s:
# run_desk.sh is idempotent (starts main.py and supervise_desk.py only if they are not running, under a lock).
# All children stay in this unit's cgroup, so `systemctl stop desk` stops the whole desk cleanly (SIGTERM).
cd "$(dirname "$0")"
while true; do
  ./run_desk.sh >/dev/null 2>>logs/keepalive.err || true
  sleep 30
done
