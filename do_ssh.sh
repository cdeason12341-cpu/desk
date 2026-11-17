#!/usr/bin/env bash
# Run a command on the always-on DigitalOcean droplet that hosts the desk (DECISION: desk moved to droplet).
# Key auth only (~/.ssh/desk_do). No secrets here. Examples:
#   ./do_ssh.sh 'systemctl status desk --no-pager'
#   ./do_ssh.sh 'tail -n 50 /opt/desk/logs/desk.log'
#   ./do_ssh.sh 'cat /opt/desk/logs/heartbeats.json'
set -euo pipefail
HOST="${DESK_DO_HOST:-165.227.20.72}"
exec ssh -i "$HOME/.ssh/desk_do" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 \
  -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 "root@$HOST" "$@"
