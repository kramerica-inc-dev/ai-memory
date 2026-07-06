#!/usr/bin/env bash
#
# mirror.sh — mirror your PROJECTS dir (source of truth) one-way to the memory host.
#
# Component A of the artifact layer. The mirrored tree can live under your host's backup
# and be readable by a file-access layer, so a semantic search result (Component B) can
# point back at a real file.
#
# Design choices:
#   - rsync (not a bidirectional sync): one-way local -> host, local stays authoritative.
#   - ADDITIVE by default (no --delete): files removed locally stay on the host. Set
#     DELETE=1 for a strict mirror.
#   - Excludes strip the noise (build output, caches, VCS) AND keep out secrets (.env, keys).
#     .env.example is kept (does not match the exact '.env' exclude).
#   - Idempotent: rsync only sends deltas.
#
# Usage:
#   migrate/mirror.sh            # real run (additive)
#   migrate/mirror.sh --dry-run  # show what would happen, write nothing
#   DELETE=1 migrate/mirror.sh   # strict mirror (removes on host what is gone locally)
#
# Override via env: SRC, DEST_HOST, DEST_PATH, MAX_SIZE, RSYNC_PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# memctl.conf (next to the repo root) provides deployment defaults; explicit env wins.
# shellcheck disable=SC1091
[ -f "$SCRIPT_DIR/../memctl.conf" ] && . "$SCRIPT_DIR/../memctl.conf"

SRC="${SRC:-${PROJECTS_DIR:-$HOME/Projects}/}"                  # trailing slash = contents, not the dir
DEST_HOST="${DEST_HOST:-${MEMORY_HOST:-user@your-memory-host}}" # SSH target
DEST_PATH="${DEST_PATH:-${ARTIFACTS_DIR:-/opt/ai-memory/artifacts}/}"
MAX_SIZE="${MAX_SIZE:-100m}"                                    # guardrail against accidental big binaries
RSYNC_PATH="${RSYNC_PATH:-rsync}"                              # rsync path ON the host (non-interactive PATH)

DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN="--dry-run"

DELETE_FLAG=""
[ "${DELETE:-0}" = "1" ] && DELETE_FLAG="--delete"

# ── excludes ────────────────────────────────────────────────────────────────
# noise (build output, caches, VCS, editor cruft)
EXCLUDES=(
  --exclude='.git/'
  --exclude='node_modules/'
  --exclude='.venv/'
  --exclude='venv/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.DS_Store'
  --exclude='graphify-out/'
  --exclude='dist/'
  --exclude='build/'
  --exclude='.next/'
  --exclude='*.log'
  --exclude='~$*'
)
# secrets — NEVER mirror (.env.example is kept; does not match '.env')
EXCLUDES+=(
  --exclude='.env'
  --exclude='.env.local'
  --exclude='.env.*.local'
  --exclude='*.pem'
  --exclude='*.key'
  --exclude='id_rsa*'
  --exclude='id_ed25519*'
  --exclude='*.p12'
  --exclude='*.keystore'
)

echo "== mirror \$PROJECTS -> host =="
echo "   src  : $SRC"
echo "   dest : $DEST_HOST:$DEST_PATH"
echo "   mode : $([ -n "$DELETE_FLAG" ] && echo 'STRICT (--delete)' || echo 'additive')${DRY_RUN:+  [DRY-RUN]}"
echo "   max  : $MAX_SIZE per file"

# Create the destination dir (rsync --mkpath is not guaranteed on every rsync version).
ssh "$DEST_HOST" "mkdir -p '$DEST_PATH'"

rsync -az --human-readable --stats --partial \
  $DRY_RUN $DELETE_FLAG \
  --max-size="$MAX_SIZE" \
  --rsync-path="$RSYNC_PATH" \
  "${EXCLUDES[@]}" \
  "$SRC" "$DEST_HOST:$DEST_PATH"

echo "== done =="
echo "Verify with:  ssh $DEST_HOST 'ls -la $DEST_PATH'"

# ── Scheduling (activate manually; not installed automatically) ──────────────
# launchd (macOS), hourly — drop as ~/Library/LaunchAgents/com.example.ai-memory.mirror.plist
# and load with: launchctl load ~/Library/LaunchAgents/com.example.ai-memory.mirror.plist
#
#   <?xml version="1.0" encoding="UTF-8"?>
#   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#   <plist version="1.0"><dict>
#     <key>Label</key><string>com.example.ai-memory.mirror</string>
#     <key>ProgramArguments</key>
#       <array><string>/path/to/ai-memory/migrate/mirror.sh</string></array>
#     <key>StartInterval</key><integer>3600</integer>
#     <key>StandardOutPath</key><string>/tmp/ai-memory-mirror.log</string>
#     <key>StandardErrorPath</key><string>/tmp/ai-memory-mirror.err</string>
#   </dict></plist>
#
# Or a Linux cron entry (hourly):
#   0 * * * * /path/to/ai-memory/migrate/mirror.sh >> /tmp/ai-memory-mirror.log 2>&1
