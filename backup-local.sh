#!/usr/bin/env bash
# backup-local.sh — SERVER-SIDE backup of the AI memory, for a local scheduler.
# Same logic as backup.sh but without SSH: it runs ON the host that runs the
# containers, so it does not depend on a workstation being awake or an SSH agent
# being unlocked (e.g. at 03:00 with a locked keychain).
#
# Synology DSM setup (one-time, GUI): Control Panel → Task Scheduler → Create →
# Scheduled task → User-defined script; daily;
# script:  bash /volume1/docker/ai-memory/backup-local.sh
# Any cron works the same way. Output lands in $DIR/backup-local.log
set -euo pipefail

DIR=${AIMEM_DIR:-/volume1/docker/ai-memory}
KEEP=${AIMEM_KEEP:-14}
D=${DOCKER:-/usr/local/bin/docker}
FALKOR=${FALKOR_SVC:-ai-memory-falkordb-1}
LOG="$DIR/backup-local.log"

exec >>"$LOG" 2>&1
echo "== backup $(date '+%Y-%m-%d %H:%M:%S') =="

# Do NOT source .env: values like bcrypt hashes contain `$2a$...`, which shell
# expansion mangles (and `set -u` turns into a hard error). Grep the one key we need.
FALKORDB_PASSWORD=$(grep -E '^FALKORDB_PASSWORD=' "$DIR/.env" | tail -1 | cut -d= -f2-)

# 1) force a snapshot and wait until it completes (LASTSAVE changes) — a fixed
#    sleep would silently copy the PREVIOUS dump when a save is slow.
LAST=$("$D" exec "$FALKOR" redis-cli -a "$FALKORDB_PASSWORD" --no-auth-warning LASTSAVE)
"$D" exec "$FALKOR" redis-cli -a "$FALKORDB_PASSWORD" --no-auth-warning BGSAVE >/dev/null
NOW="$LAST"
for _ in $(seq 1 60); do
  sleep 2
  NOW=$("$D" exec "$FALKOR" redis-cli -a "$FALKORDB_PASSWORD" --no-auth-warning LASTSAVE)
  [ "$NOW" != "$LAST" ] && break
done
if [ "$NOW" = "$LAST" ]; then
  echo "ERROR: BGSAVE did not finish within 120s — no copy made"
  exit 1
fi

# 2) timestamped copy + retention (keep the last $KEEP)
mkdir -p "$DIR/backups"
cp "$DIR/data/falkordb/dump.rdb" "$DIR/backups/dump-$(date +%Y%m%d-%H%M%S).rdb"
ls -1t "$DIR"/backups/dump-*.rdb | tail -n +$((KEEP+1)) | xargs -r rm -f

echo "ok — newest: $(ls -1t "$DIR"/backups/dump-*.rdb | head -1)"
