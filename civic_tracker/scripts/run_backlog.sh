#!/usr/bin/env bash
# (Superseded by the civic-worker service; kept for one-off runs.)
# Unattended backlog run: pull up to N recent articles from every ACTIVE source, then run the
# full Stage 1 -> Stage 2 -> Resolver pipeline until nothing is PENDING.
#
#   scripts/run_backlog.sh [N=20] [DAYS=365]
#
# Detach so it survives the terminal:  setsid nohup scripts/run_backlog.sh 20 > /dev/null 2>&1 &
# Progress: tail -f logs/backlog_*.log   Status: scripts/status.sh
set -uo pipefail
cd "$(dirname "$0")/.."
N="${1:-20}"
DAYS="${2:-365}"
CT=".venv/bin/civic-tracker"
mkdir -p logs
LOG="logs/backlog_$(date +%Y%m%d_%H%M%S).log"
exec >>"$LOG" 2>&1

echo "=== $(date -Is) backlog start: last $N articles per active source, window $DAYS days"
$CT crawl --backfill-days "$DAYS" --limit "$N" || echo "!!! crawl exited with $?"

echo "=== $(date -Is) processing"
# `process` loops until no PENDING/STAGE1_DONE rows remain; per-article failures are marked FAILED.
$CT process --concurrency 6 || echo "!!! process exited with $?"

echo "=== $(date -Is) backlog done"
