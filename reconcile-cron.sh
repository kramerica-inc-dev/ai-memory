#!/usr/bin/env bash
# reconcile-cron.sh — NAS-SIDE scheduled wrapper around migrate/reconcile.py.
# Runs ON the NAS itself (no SSH, no dependency on a workstation being awake or an
# SSH agent being unlocked). Designed for the Synology DSM Task Scheduler, but any
# cron works.
#
# DSM setup (one-time, GUI): Control Panel → Task Scheduler → Create → Scheduled
# task → User-defined script; user: <your user>; daily;
# script:  bash /volume1/docker/ai-memory/reconcile-cron.sh
# Output lands in $DIR/reconcile.log.
#
# ALERTING: enable "Send run details by email" + "only when the script terminates
# abnormally" on the DSM task — findings/critical exit nonzero, so that mails you.
# (synodsmnotify is useless here: DSM only accepts pre-registered i18n title keys.)
#
# Exit codes follow reconcile.py: 0 = clean · 1 = findings · 2 = critical.
set -uo pipefail

DIR=${AIMEM_DIR:-/volume1/docker/ai-memory}
LOG="$DIR/reconcile.log"

exec >>"$LOG" 2>&1
echo "== reconcile $(date '+%Y-%m-%d %H:%M:%S') =="

# reconcile.py runs locally on the NAS: empty MEMORY_HOST = local docker.
export MEMORY_HOST=""
export MEMORY_DIR="$DIR"
export DOCKER=${DOCKER:-/usr/local/bin/docker}
export MAPPING_CONFIG="$DIR/config/mapping.yaml"

python3 "$DIR/migrate/reconcile.py" --fix
rc=$?

if [ "$rc" -ne 0 ]; then
  sev=$([ "$rc" -ge 2 ] && echo "CRITICAL" || echo "findings")
  echo "reconcile exit=$rc ($sev) — see above"
else
  echo "ok — all invariants hold"
fi

# keep the log bounded (~last 2000 lines)
tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit "$rc"
