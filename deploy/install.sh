#!/usr/bin/env bash
# Install + start the Civic Wiki (gunicorn) and pipeline worker as systemd services.
# Prerequisites:
#   1. sql/unapplied/v1.2_WebUI.sql applied to the database
#   2. admin password set:  cd /root && .venv/bin/flask --app index set-admin-password
set -euo pipefail
cd "$(dirname "$0")/.."

# Stop the one-off backlog script if it's still running; the worker takes over its queue
# (any article it was mid-way through is released back to the queue on worker start).
if pgrep -f "civic_tracker/scripts/run_backlog.sh|civic-tracker process" >/dev/null; then
  echo "Stopping the old backlog run..."
  pkill -TERM -f "civic-tracker process" || true
  pkill -TERM -f "scripts/run_backlog.sh" || true
  sleep 3
fi

install -m 0644 deploy/civic-web.service /etc/systemd/system/civic-web.service
install -m 0644 deploy/civic-worker.service /etc/systemd/system/civic-worker.service
systemctl daemon-reload
systemctl enable --now civic-worker civic-web
systemctl --no-pager --lines=5 status civic-worker civic-web
echo
echo "Wiki: http://$(hostname -I | awk '{print $1}'):8080/   Admin: /admin   Logs: journalctl -u civic-worker -f"
