#!/usr/bin/env bash
# memctl.sh — control plane for the self-hosted, provider-agnostic AI-memory stack.
#
# Cheap LLM-provider swaps, a guarded (safe) embedder reindex, and health checks — without ever
# hand-editing the mounted config.yaml. Content-free by design: every deployment specific comes
# from configuration, never from this script.
#
# Configuration — env vars, or a local ./memctl.conf sourced first if present (keep it gitignored):
#   MEMORY_HOST    SSH target "user@host" for a remote deploy; empty/unset = operate on local docker.
#   MEMORY_DIR     Dir holding docker-compose.yml + .env on the target (default: this script's dir).
#   MEMORY_GROUPS  Space-separated namespaces (group_ids) to operate on (needed for status/reindex).
#   COMPOSE        Compose command on the target (default: "docker compose").
#   FALKOR_SVC     FalkorDB container name (default: ai-memory-falkordb-1).
#   MCP_SVC        graphiti-mcp container name (default: ai-memory-graphiti-mcp-1).
#   MCP_COMPOSE_SVC  graphiti-mcp compose service name (default: graphiti-mcp).
#   MCP_URL, SANITIZER_URL  passed through to the migrate/ scripts during reindex.
#
# Usage:
#   ./memctl.sh doctor
#   ./memctl.sh status
#   ./memctl.sh switch <profile>
#   ./memctl.sh reindex --to <profile> [--groups a,b,c] [--dry-run] [--yes]
#   ./memctl.sh ingest <chunks.jsonl> [--groups a,b] [--batch N]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[ -f "$SCRIPT_DIR/memctl.conf" ] && . "$SCRIPT_DIR/memctl.conf"

MEMORY_HOST="${MEMORY_HOST:-}"
MEMORY_DIR="${MEMORY_DIR:-$SCRIPT_DIR}"
DOCKER="${DOCKER:-docker}"
COMPOSE="${COMPOSE:-docker compose}"
FALKOR_SVC="${FALKOR_SVC:-ai-memory-falkordb-1}"
MCP_SVC="${MCP_SVC:-ai-memory-graphiti-mcp-1}"
MCP_COMPOSE_SVC="${MCP_COMPOSE_SVC:-graphiti-mcp}"
MEMORY_GROUPS="${MEMORY_GROUPS:-}"
PROFILES_DIR="$SCRIPT_DIR/profiles"

# ── execution: run a shell snippet on the target (remote over SSH, or locally) ───────────────
# MEMORY_SSH_OPTS: extra ssh/scp options (e.g. a specific -i key, or bypassing an ssh agent).
onhost() {
  if [ -n "$MEMORY_HOST" ]; then
    # shellcheck disable=SC2086 — MEMORY_SSH_OPTS is intentionally word-split into flags
    ssh ${MEMORY_SSH_OPTS:-} -o ConnectTimeout=15 -o LogLevel=ERROR "$MEMORY_HOST" "$1"
  else
    bash -c "$1"
  fi
}

# GRAPH.QUERY on FalkorDB. The password is sourced from the TARGET's .env at call time and never
# leaves the target (never printed, never stored locally). Keep queries free of double quotes.
redis_query() {
  local g="$1" q="$2"
  onhost "set -a; . '$MEMORY_DIR/.env'; set +a; $DOCKER exec $FALKOR_SVC redis-cli -a \"\$FALKORDB_PASSWORD\" --no-auth-warning GRAPH.QUERY $g '$q' 2>/dev/null"
}

# Arbitrary redis command on the FalkorDB container (same auth path as redis_query).
redis_cmd() {
  onhost "set -a; . '$MEMORY_DIR/.env'; set +a; $DOCKER exec $FALKOR_SVC redis-cli -a \"\$FALKORDB_PASSWORD\" --no-auth-warning $1 2>/dev/null"
}

# ── embedding-space marker: records which vector space the stored graphs were built in ──────
# Written on every switch/reindex; doctor (and migrate/reconcile.py) alarm on drift between
# this marker and the live embedder env. Catches the "bare docker compose up -d regressed the
# embedder → vector dimension mismatch" class of incident before it corrupts search.
SPACE_KEY="aimem:embedding_space"
set_space_marker() { redis_cmd "SET $SPACE_KEY $1" >/dev/null; }
get_space_marker() { redis_cmd "GET $SPACE_KEY" | tr -d '\r'; }

mcp_env() { onhost "$DOCKER exec $MCP_SVC printenv" 2>/dev/null; }
active_profile() { onhost "cat '$MEMORY_DIR/.active-profile' 2>/dev/null" || true; }
profile_val() { grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
envval() { grep -E "^$1=" <<<"$2" | head -1 | cut -d= -f2- || true; }
list_profiles() { echo "  available profiles:"; for f in "$PROFILES_DIR"/*.env; do echo "    - $(basename "$f" .env)"; done; }
groups_or_die() { [ -n "$MEMORY_GROUPS" ] || { echo "✗ MEMORY_GROUPS is unset — set it in memctl.conf (space-separated namespaces)."; exit 1; }; }

# ── doctor: read the live container env, assert dimension consistency + required keys ────────
cmd_doctor() {
  echo "▸ doctor — live env from $MCP_SVC${MEMORY_HOST:+ @ $MEMORY_HOST}"
  local env bad=""; env="$(mcp_env)" || { echo "  ✗ cannot reach $MCP_SVC"; return 1; }
  local llm emb model emodel edim edim2 obase
  llm=$(envval LLM_PROVIDER "$env");            emb=$(envval EMBEDDER_PROVIDER "$env")
  model=$(envval MODEL_NAME "$env");            emodel=$(envval EMBEDDER_MODEL "$env")
  edim=$(envval EMBEDDER_DIMENSIONS "$env");    edim2=$(envval EMBEDDING_DIM "$env")
  obase=$(envval OPENAI_BASE_URL "$env")
  local ap; ap="$(active_profile)"; echo "  active profile : ${ap:-(none marked)}"
  echo "  LLM            : provider=${llm:-'(config default)'}  model=${model:-?}  base=${obase:-?}"
  echo "  embedder       : provider=${emb:-'(config default)'}  model=${emodel:-?}  dims=${edim:-?}"
  if [ -n "$edim" ] && [ -n "$edim2" ] && [ "$edim" != "$edim2" ]; then
    echo "  ✗ DIM MISMATCH: EMBEDDER_DIMENSIONS=$edim vs EMBEDDING_DIM=$edim2 → vector-search breaks after restart"; bad=1
  else
    echo "  ✓ dims consistent (EMBEDDER_DIMENSIONS == EMBEDDING_DIM == ${edim:-?})"
  fi
  local marker want; marker="$(get_space_marker)"; want="$emodel/$edim"
  if [ -z "$marker" ]; then
    echo "  · embedding-space marker not set — run switch (or reindex) once to record it"
  elif [ "$marker" != "$want" ]; then
    echo "  ✗ EMBEDDING-SPACE DRIFT: graphs were built in '$marker' but live embedder is '$want'"
    echo "    → stored vectors are invalid for this embedder. Fix the env/profile (switch) or reindex."; bad=1
  else
    echo "  ✓ embedding space matches stored graphs ($marker)"
  fi
  key_present() { grep -qE "^$1=.+" <<<"$env"; }
  local rurl; rurl=$(envval RERANKER_URL "$env")
  [ -n "$rurl" ] && echo "  ✓ reranker: local via $rurl ($(envval RERANKER_MODEL "$env"))"
  case "$obase" in
    *api.openai.com*|"")
      if key_present OPENAI_API_KEY; then echo "  ✓ OPENAI_API_KEY present"
      elif [ "$llm" = openai ]; then echo "  ✗ OPENAI_API_KEY missing — openai extraction needs it"; bad=1
      elif [ -z "$rurl" ]; then echo "  ✗ OPENAI_API_KEY missing — the default (OpenAI) reranker needs it"; bad=1
      else echo "  · OPENAI_API_KEY absent (ok: local reranker + non-openai LLM)"; fi ;;
    *) echo "  · OPENAI_BASE_URL is non-default ($obase) — openai-routed calls go there" ;;
  esac
  [ "$emb" = gemini ]    && { key_present GOOGLE_API_KEY    && echo "  ✓ GOOGLE_API_KEY present (gemini embedder)"  || { echo "  ✗ GOOGLE_API_KEY missing";    bad=1; }; }
  [ "$llm" = anthropic ] && { key_present ANTHROPIC_API_KEY && echo "  ✓ ANTHROPIC_API_KEY present"                || { echo "  ✗ ANTHROPIC_API_KEY missing"; bad=1; }; }
  [ "$llm" = gemini ]    && { key_present GOOGLE_API_KEY    && echo "  ✓ GOOGLE_API_KEY present (gemini llm)"      || { echo "  ✗ GOOGLE_API_KEY missing";    bad=1; }; }
  [ -n "$bad" ] && { echo "  → doctor found problems"; return 1; }
  echo "  ✓ doctor OK"
}

# ── status: active profile + per-namespace episode counts ───────────────────────────────────
cmd_status() {
  groups_or_die
  echo "▸ status — active profile: $(active_profile)"
  for g in $MEMORY_GROUPS; do
    local out tot
    out=$(redis_query "$g" "MATCH (e:Episodic) RETURN count(e)" || true)
    tot=$(grep -Eo '[0-9]+' <<<"$out" | head -1)
    printf '  %-14s episodes=%s\n' "$g" "${tot:-0}"
  done
}

# ── deploy the profile to the target and (re)create the graphiti-mcp container ───────────────
deploy_and_up() {
  local profile="$1"
  if [ -n "$MEMORY_HOST" ]; then
    # config.yaml (env-driven provider:) + docker-compose.yml (the LLM_PROVIDER/EMBEDDER_PROVIDER
    # environment lines) + profiles/ must all be present on the target for the switch to land.
    # tar-over-ssh (not rsync) so it reuses the exact ssh path that already authenticates.
    # shellcheck disable=SC2086 — MEMORY_SSH_OPTS is intentionally word-split into flags
    tar -C "$SCRIPT_DIR" -czf - config.yaml docker-compose.yml profiles \
      | ssh ${MEMORY_SSH_OPTS:-} -o ConnectTimeout=15 -o LogLevel=ERROR "$MEMORY_HOST" "tar -xzf - -C '$MEMORY_DIR'"
  fi
  onhost "cd '$MEMORY_DIR' && cat .env 'profiles/$profile.env' > .env.active && echo '$profile' > .active-profile && $COMPOSE --env-file .env.active up -d $MCP_COMPOSE_SVC"
}

# ── switch: cheap LLM-provider swap; refuses embedder changes (those need reindex) ───────────
cmd_switch() {
  local profile="" force=""
  while [ $# -gt 0 ]; do case "$1" in --force) force=1; shift;; *) profile="$1"; shift;; esac; done
  [ -n "$profile" ] || { echo "✗ usage: memctl switch [--force] <profile>"; list_profiles; return 1; }
  local pf="$PROFILES_DIR/$profile.env"
  [ -f "$pf" ] || { echo "✗ unknown profile '$profile'"; list_profiles; return 1; }
  local env; env="$(mcp_env)" || true
  # Vector compatibility is determined by embedder MODEL + DIMENSIONS. A change there invalidates
  # every stored vector → must go through reindex, not a hot switch. (Provider alone isn't compared:
  # legacy containers may not expose EMBEDDER_PROVIDER, and same-model is the same vector space.)
  local lem led; lem=$(envval EMBEDDER_MODEL "$env"); led=$(envval EMBEDDER_DIMENSIONS "$env")
  local tem ted; tem=$(profile_val "$pf" EMBEDDER_MODEL); ted=$(profile_val "$pf" EMBEDDER_DIMENSIONS)
  if [ -z "$force" ] && [ -n "$lem$led" ] && { [ "${tem:-$lem}" != "$lem" ] || [ "${ted:-$led}" != "$led" ]; }; then
    echo "✗ profile '$profile' changes the EMBEDDER model/dims:"
    echo "    $lem / ${led}d   →   $tem / ${ted}d"
    echo "  This invalidates stored vectors → use:  ./memctl.sh reindex --to $profile"
    echo "  (or --force if the target embedder's existing vectors are already valid, e.g. restoring)"
    return 1
  fi
  echo "▸ switch → $profile (LLM-only; embedder unchanged, vectors stay valid)"
  deploy_and_up "$profile"
  set_space_marker "${tem:-$lem}/${ted:-$led}"
  echo "✓ switched to '$profile'. Verify with:  ./memctl.sh doctor"
}

# ── reindex: the safe embedder swap — orchestrates existing scripts, destructive (guarded) ───
cmd_reindex() {
  local profile="" only_groups="" dry="" yes=""
  while [ $# -gt 0 ]; do case "$1" in
    --to) profile="$2"; shift 2;;
    --groups) only_groups="${2//,/ }"; shift 2;;
    --dry-run) dry=1; shift;;
    --yes) yes=1; shift;;
    *) echo "✗ unknown arg: $1"; return 1;;
  esac; done
  [ -n "$profile" ] || { echo "✗ usage: memctl reindex --to <profile> [--groups a,b] [--dry-run] [--yes]"; return 1; }
  local pf="$PROFILES_DIR/$profile.env"; [ -f "$pf" ] || { echo "✗ unknown profile '$profile'"; list_profiles; return 1; }
  groups_or_die
  local groups="${only_groups:-$MEMORY_GROUPS}"
  echo "▸ reindex → $profile"
  echo "  target embedder : $(profile_val "$pf" EMBEDDER_PROVIDER) / $(profile_val "$pf" EMBEDDER_MODEL) / $(profile_val "$pf" EMBEDDER_DIMENSIONS)d"
  echo "  namespaces      : $groups"
  echo "  steps: backup → wipe graphs → switch env+restart → reset ledgers → backfill+index → verify"
  if [ -n "$dry" ]; then echo "  (dry-run) no changes made."; return 0; fi
  if [ -z "$yes" ]; then
    printf "  This WIPES and re-embeds the namespaces above. Type the word 'reindex' to proceed: "
    local ans; read -r ans; [ "$ans" = reindex ] || { echo "  aborted."; return 1; }
  fi
  echo "  [1/6] backup"; "$SCRIPT_DIR/backup.sh"
  echo "  [2/6] wipe graphs"; for g in $groups; do redis_query "$g" "MATCH (n) DETACH DELETE n" >/dev/null; echo "    wiped $g"; done
  # Wiped content must be re-ingestable: clear the durable queue's idempotency set too,
  # or every re-ingested episode would be skipped as "already processed".
  redis_cmd "DEL aimem:processed" >/dev/null; echo "    cleared idempotency set"
  echo "  [3/6] switch env + restart ($profile)"; deploy_and_up "$profile"
  set_space_marker "$(profile_val "$pf" EMBEDDER_MODEL)/$(profile_val "$pf" EMBEDDER_DIMENSIONS)"
  echo "  [4/6] reset ledgers"; rm -f "$SCRIPT_DIR/migrate/.indexed.json" "$SCRIPT_DIR/migrate/.backfilled.txt" "$SCRIPT_DIR/migrate/.indexed.lock"
  echo "  [5/6] backfill + index (this is the slow part)"
  ( cd "$SCRIPT_DIR" && python3 migrate/backfill.py --source all && python3 migrate/index_files.py --all --docs-only )
  echo "  [6/6] verify"; ( cd "$SCRIPT_DIR" && python3 migrate/verify_index.py ) || true
  echo "✓ reindex complete. Run ./memctl.sh doctor && ./memctl.sh status."
}

# ── ingest: supervised mass-ingest launcher (attached; bulk_ingest holds the writer lock) ────
# The ONLY sanctioned way to run bulk_ingest. Runs ATTACHED (never `docker exec -d`): a detached
# exec gets reaped and a stall is indistinguishable from progress — exactly what caused the
# duplicate-writer incident. bulk_ingest.py enforces single-writer (Redis aimem:writer-lock),
# idempotency (aimem:processed) and dead-letter (aimem:dead) itself; this just ships the files
# in and streams the run.
cmd_ingest() {
  local jsonl="" groups="" batch="25"
  while [ $# -gt 0 ]; do case "$1" in
    --groups) groups="$2"; shift 2;;
    --batch)  batch="$2"; shift 2;;
    *)        jsonl="$1"; shift;;
  esac; done
  [ -n "$jsonl" ] && [ -f "$jsonl" ] || { echo "✗ usage: memctl ingest <chunks.jsonl> [--groups a,b] [--batch N]"; return 1; }
  local base; base="$(basename "$jsonl")"
  echo "▸ ingest — $jsonl  (groups=${groups:-all}, batch=$batch, attached/single-writer)"
  if [ -n "$MEMORY_HOST" ]; then
    # shellcheck disable=SC2086
    scp -O ${MEMORY_SSH_OPTS:-} -o ConnectTimeout=15 -o LogLevel=ERROR "$jsonl" "$MEMORY_HOST:$MEMORY_DIR/$base"
    # shellcheck disable=SC2086
    scp -O ${MEMORY_SSH_OPTS:-} -o ConnectTimeout=15 -o LogLevel=ERROR "$SCRIPT_DIR/migrate/bulk_ingest.py" "$MEMORY_HOST:$MEMORY_DIR/bulk_ingest.py"
  else
    cp "$jsonl" "$MEMORY_DIR/$base"; cp "$SCRIPT_DIR/migrate/bulk_ingest.py" "$MEMORY_DIR/bulk_ingest.py"
  fi
  onhost "$DOCKER cp '$MEMORY_DIR/$base' $MCP_SVC:/tmp/$base && $DOCKER cp '$MEMORY_DIR/bulk_ingest.py' $MCP_SVC:/tmp/bulk_ingest.py"
  local args="/tmp/$base --batch $batch"
  [ -n "$groups" ] && args="$args --groups $groups"
  onhost "$DOCKER exec $MCP_SVC /app/mcp/.venv/bin/python -u /tmp/bulk_ingest.py $args"
}

case "${1:-}" in
  doctor)  cmd_doctor ;;
  status)  cmd_status ;;
  switch)  shift; cmd_switch "$@" ;;
  reindex) shift; cmd_reindex "$@" ;;
  ingest)  shift; cmd_ingest "$@" ;;
  *) echo "memctl.sh — doctor | status | switch <profile> | reindex --to <profile> [--groups a,b] [--dry-run] [--yes] | ingest <chunks.jsonl> [--groups a,b] [--batch N]"; list_profiles ;;
esac
