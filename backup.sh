#!/usr/bin/env bash
# On-demand backup of the AI memory: forces a FalkorDB RDB snapshot and keeps a timestamped
# copy on the memory host (retention = last N). Run it locally against a local docker instance,
# or against a remote host over SSH.
#
#     ./backup.sh
#
# Config (env, or via memctl.conf which is sourced if present next to this script):
#   MEMORY_HOST   SSH target "user@host"; empty = local docker.
#   MEMORY_DIR    Dir holding docker-compose.yml + .env + data/ (default: /opt/ai-memory).
#   DOCKER        Docker binary on the target (default: docker).
#   FALKOR_SVC    FalkorDB container name (default: ai-memory-falkordb-1).
#   KEEP          How many snapshots to retain (default: 14).
#
# The MEMORY_DIR/backups directory should also be part of your host's off-site backup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[ -f "$SCRIPT_DIR/memctl.conf" ] && . "$SCRIPT_DIR/memctl.conf"

MEMORY_HOST="${MEMORY_HOST:-}"
MEMORY_DIR="${MEMORY_DIR:-/opt/ai-memory}"
DOCKER="${DOCKER:-docker}"
FALKOR_SVC="${FALKOR_SVC:-ai-memory-falkordb-1}"
KEEP="${KEEP:-14}"

REMOTE_SCRIPT=$(cat <<REMOTE
set -e
DIR="$MEMORY_DIR"
KEEP=$KEEP
set -a; . "\$DIR/.env"; set +a
D="$DOCKER"
FALKOR="$FALKOR_SVC"

# 1) force a snapshot to dump.rdb and WAIT until it completes (LASTSAVE changes).
#    A fixed sleep silently copies the PREVIOUS dump when the save takes longer.
LAST=\$("\$D" exec "\$FALKOR" redis-cli -a "\$FALKORDB_PASSWORD" --no-auth-warning LASTSAVE)
"\$D" exec "\$FALKOR" redis-cli -a "\$FALKORDB_PASSWORD" --no-auth-warning BGSAVE >/dev/null
NOW="\$LAST"
for _ in \$(seq 1 60); do
  sleep 2
  NOW=\$("\$D" exec "\$FALKOR" redis-cli -a "\$FALKORDB_PASSWORD" --no-auth-warning LASTSAVE)
  [ "\$NOW" != "\$LAST" ] && break
done
if [ "\$NOW" = "\$LAST" ]; then
  echo "BGSAVE did not complete within 120s — aborting, no copy made" >&2
  exit 1
fi

# 2) timestamped copy + retention
mkdir -p "\$DIR/backups"
cp "\$DIR/data/falkordb/dump.rdb" "\$DIR/backups/dump-\$(date +%Y%m%d-%H%M%S).rdb"
ls -1t "\$DIR"/backups/dump-*.rdb | tail -n +\$((KEEP+1)) | xargs -r rm -f

echo "backup ok — latest snapshots:"
ls -lh "\$DIR/backups/" | tail -5
REMOTE
)

if [ -n "$MEMORY_HOST" ]; then
  # shellcheck disable=SC2086 — MEMORY_SSH_OPTS is intentionally word-split into flags
  ssh ${MEMORY_SSH_OPTS:-} "$MEMORY_HOST" "bash -s" <<<"$REMOTE_SCRIPT"
else
  bash -c "$REMOTE_SCRIPT"
fi
