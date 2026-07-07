# Deploy & configuration

This covers deploying the stack and configuring the three provider layers, the embedder options,
and hardening. See `README.md` for the overview and `ARCHITECTURE.md` for the design rationale.

## Prerequisites

- Docker + Docker Compose on the memory host (a small always-on box: NAS, mini-PC, VPS, ...).
- A private network between clients and the host (e.g. Tailscale/WireGuard). The MCP port has no
  auth — do not expose it publicly.
- An API key for at least one LLM provider and one embedder.

## Configuration files

| File | Purpose | Committed? |
|---|---|---|
| `.env` | secrets + runtime knobs (keys, FalkorDB password, image pins) | no (gitignored) |
| `config/mapping.yaml` | your namespaces + directory/project routing | no (gitignored) |
| `memctl.conf` | control-plane target (host, dir, namespaces, endpoints) | no (gitignored) |
| `config.yaml` | Graphiti stack config (env-driven, provider-agnostic) | yes |
| `profiles/*.env` | provider presets for `memctl switch` / `reindex` | yes |

Start by copying the three examples:

```bash
cp .env.example .env
cp config/mapping.example.yaml config/mapping.yaml
cp memctl.conf.example memctl.conf
```

### `.env`

Fill in at minimum:
- one LLM key (`GOOGLE_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`),
- `OPENAI_API_KEY` if you use the reranker or OpenAI extraction (the reranker needs a logprobs
  endpoint; OpenAI-compatible),
- a strong `FALKORDB_PASSWORD`.

Pin images to a digest for reproducibility after the first pull:

```bash
docker images --digests | grep -E 'falkordb|knowledge-graph-mcp'
# put the falkordb digest into .env as FALKORDB_IMAGE
```

### `config/mapping.yaml` (the content layer)

This is the only place your taxonomy lives. `default_group`, the list of `groups` (namespaces),
and a `group_map` (source key -> group). Both ingest adapters read it:
- `index_files.py`: key = top-level directory under `PROJECTS_DIR`,
- `backfill.py`: key = the project/label of a claude-mem-style SQLite source.

The scripts fail closed if this file is missing. Override the path with `MAPPING_CONFIG`.

## First deploy

```bash
docker compose up -d
curl -sS http://localhost:8000/mcp/ -H 'Accept: text/event-stream' -i | head   # expect SSE 200
```

For a remote host, set `MEMORY_HOST`/`MEMORY_DIR` in `memctl.conf`; `memctl.sh` ships config and
recreates the container over SSH. Everyday management is plain compose:

```bash
docker compose ps
docker compose logs -f graphiti-mcp
docker compose up -d      # apply .env / config changes
docker compose down
```

## Provider layers

Selection is env-driven; you never hand-edit `config.yaml`. Presets live in `profiles/`:

| Profile | Extraction LLM | Embedder | Reindex on switch? |
|---|---|---|---|
| `openai-cloud` | OpenAI | Gemini | no |
| `gemini` | Gemini | Gemini | no |
| `claude` | Anthropic | Gemini | no |
| `local-embedder` | OpenAI | bge-m3 via infinity (1024d) | **yes** |
| `local-embedder-remote` | OpenAI | bge-m3 on a second machine | no (same vector space) |
| `local-ollama` | Ollama (via gateway) | Gemini | no |

### Choosing your models

The specific model names shipped in this repo (`gpt-5.4-mini` for extraction, `claude-sonnet-5`,
`gemini-2.5-flash`, `qwen2.5:14b`, `bge-m3` for embeddings) are **only examples/defaults** — the
mix the author happened to run. **You choose your own provider + model.** Nothing is hard-coded:
override per profile (`profiles/*.env` → `MODEL_NAME` / `EMBEDDER_MODEL`) or per environment
(`.env` / compose), then `./memctl.sh switch <profile>`. Pick by your own constraints — cost,
latency, data residency, and how reliably a model produces schema-valid structured output (weaker
models drop more episodes during extraction). The embedder is the one to commit to up front: its
model + dimension is baked into the vector index, so changing it later means a full `reindex`.

```bash
./memctl.sh doctor            # show live providers + assert key presence & dimension consistency
./memctl.sh switch gemini     # cheap LLM-only swap (embedder unchanged)
./memctl.sh status            # per-namespace episode counts
./memctl.sh reindex --to local-embedder --dry-run   # preview the destructive embedder swap
```

**Switch vs reindex:** a `switch` changes only the extraction LLM; the embedder (and thus stored
vectors) stay valid. Changing the embedder **model or dimension** invalidates every vector, so it
must go through `reindex` (backup -> wipe graphs -> switch env -> reset ledgers -> re-ingest ->
verify). `switch` refuses an embedder change unless you pass `--force` (e.g. restoring a matching
vector space).

## Embedder options

- **Cloud** — Gemini (`gemini-embedding-001`, 768d) or OpenAI embeddings. Simplest; no extra
  container. Set `EMBEDDER_PROVIDER` + keys.
- **Local / vendor-free** — any OpenAI-compatible `/v1/embeddings` endpoint. This repo ships an
  `infinity` service running **BAAI/bge-m3** (multilingual, 1024d, CPU-friendly); the model cache
  persists in a volume so the ~2.3GB download happens once. Ollama's embeddings endpoint works too.
  Point `EMBEDDER_OPENAI_URL` at it and use `EMBEDDER_PROVIDER=openai`.
- A remote embedder on a faster machine (behind NAT) can be reached over a reverse tunnel — see
  `profiles/local-embedder-remote.env`.

> Keep `EMBEDDER_DIMENSIONS` and `EMBEDDING_DIM` equal, and remember any embedder model/dimension
> change requires a full reindex.

## Fully-local LLM (no data to a cloud)

Run Ollama (structured output needs a capable model, e.g. `qwen2.5:14b`), set
`profiles/local-ollama.env`, and ensure the memory host can reach the Ollama host. Note: some
standalone images force the OpenAI Responses API for `provider=openai`, which Ollama's Chat
Completions endpoint does not speak (404) — verify, or route via an OpenAI-compatible gateway
(e.g. LiteLLM).

## FalkorDB operational notes

- **Durability (AOF).** The compose file enables explicit RDB savepoints plus an append-only
  file (`--appendonly yes --appendfsync everysec`): a crash costs at most ~1s of writes instead
  of everything since the last `BGSAVE`. The AOF lives in `appendonlydir/` next to `dump.rdb`
  in the data bind-mount, so file-level backups cover both.
  **⚠ Upgrading from an RDB-only data dir — and restoring from a `dump.rdb` backup:** booting
  with `--appendonly yes` on a data dir that has only `dump.rdb` makes Redis **ignore the RDB
  and start empty**. Safe order: boot once *without* appendonly (RDB loads) → runtime
  `CONFIG SET appendonly yes` → wait until `aof_rewrite_in_progress:0` → then recreate with the
  flag. Verify with `INFO persistence` (`aof_enabled:1`).
- **Query timeout.** The image default `TIMEOUT 1000` is a hard 1s limit on read queries;
  Graphiti's dedup fulltext queries grow with the graph and start timing out ("Query timed out",
  episodes silently not landing). The compose file replaces it with the newer mechanism and
  generous limits (`TIMEOUT_DEFAULT 300000 TIMEOUT_MAX 600000` — they do not coexist with the
  deprecated `TIMEOUT`): on a large group a single `add_episode` dedup can take minutes.
  To apply live without a restart: `GRAPH.CONFIG SET TIMEOUT 0` + `TIMEOUT_MAX`/`TIMEOUT_DEFAULT`
  via `redis-cli`.
- **Post-reboot search.** After a reboot semantic search on a group can return 0 until the first
  write to that group rebuilds its vector index (self-heal). A warm-up `add_memory` per active
  group closes the gap.
- **Rate limits.** Free provider tiers have low RPM/day limits; under load you will see `429` and
  episodes drop. Lower `SEMAPHORE_LIMIT`, use a paid tier, or wait for the quota to reset.

## Backup & retention

- FalkorDB persists to `data/falkordb/dump.rdb` (RDB snapshots). Include `data/` and `backups/` in
  your host's off-site backup.
- `./backup.sh` forces a `BGSAVE` and keeps the last `KEEP` timestamped copies under
  `MEMORY_DIR/backups/` (works locally or over SSH via `MEMORY_HOST`). Wire it into cron if you want
  a nightly snapshot.
- `./backup-local.sh` is the **server-side** variant for a scheduler on the docker host itself
  (e.g. Synology DSM Task Scheduler): no SSH, so it does not depend on a workstation being awake
  or an SSH agent being unlocked at 03:00.
- `./reconcile-cron.sh` wraps `migrate/reconcile.py --fix` the same way: schedule it daily to get
  self-heal plus alerting on invariant violations, dead-lettered episodes, and queue backlog
  (nonzero exit → let your scheduler mail you the run details). It suppresses the orphan sweep
  while an ingest is active (writer lock held or backlog > 0), so a scheduled run never deletes
  mid-extraction entities.

## Mass ingest / onboarding

Large imports run through **`./memctl.sh ingest <chunks.jsonl> [--groups a,b] [--batch N]`** —
the only sanctioned launcher for `migrate/bulk_ingest.py`. It runs the tool **attached** (never
detached), and the tool enforces the same guarantees as the daily MCP path:
- **Single writer** via a Redis lock `aimem:writer-lock` (a second run refuses; the durable-queue
  consumer pauses while it is held, so the two never write the same graph concurrently);
- **Idempotent** via `aimem:processed` (the same key the queue uses) — re-runs/overlaps are no-ops;
- **No silent loss** — a batch that fails after retries is written to `aimem:dead`, not dropped;
- **Observable** — `aimem:heartbeat:bulk`, and the consumer heartbeat `aimem:heartbeat:consumer`
  that `reconcile` reports (a stale `processing` heartbeat past `CONSUMER_STALL_WARN` is a stall).

**Always clear `aimem:processed` together with a graph wipe** (`memctl reindex` does; a manual
`GRAPH.DELETE` does not) — otherwise re-ingest is a no-op and the graph stays empty.
- Forgetting: Graphiti supersedes facts automatically; manual removal via `delete_episode`,
  `delete_entity_edge`, or wiping a whole group (`clear_graph` / FalkorDB `GRAPH.DELETE <group>`).
  After a `GRAPH.DELETE`, re-populate the group (a write rebuilds its search index).

## Hygiene gate (the sanitizer)

Every ingest path (`backfill.py`, `index_files.py`, and any capture hook) sends text through a
secret-sanitizer **before** it reaches the extraction LLM or the graph, so credentials and PII
never leak into memory. It is **fail-closed**: if the sanitizer is unreachable, nothing is
ingested (unless you explicitly pass `--no-sanitize`, which is only for throwaway tests).

The gate also covers the **MCP write path** (`add_memory` from any client): when `SANITIZER_URL`
is set on the `graphiti-mcp` service, the durable queue scrubs episode content at consume time,
before extraction. Fail-closed here too — a sanitizer failure classifies as transient (backoff,
then dead-letter), never an unscrubbed pass-through. Leave `SANITIZER_URL` empty to disable the
gate (the queue then logs a warning at startup).

The tool expects a simple HTTP contract: `POST /api/sanitize` with `{"text": ..., "depth": ...}`
returning `{"sanitized": ...}` (`depth` is `quick` | `standard` | `deep`). A ready-made service
that implements exactly this is **secret-sanitizer**
([github.com/kramerica-inc-dev/secret-sanitizer](https://github.com/kramerica-inc-dev/secret-sanitizer)) —
run it somewhere reachable and set `SANITIZER_URL` to its base URL. You can also bring your own
service that speaks the same contract. If you expose an external sanitizer to clients over the
tailnet, the Caddy `:8080` block can proxy it (see `Caddyfile`).

## Secret management

**Principle:** secrets that the sanitizer detects (API keys, tokens, passwords) must **not** go
into the knowledge graph. They belong in a dedicated secret manager; only an opaque **reference**
to the secret should ever be stored in memory. That way "the agent knows a credential exists and
where to fetch it" without the value ever touching the graph or an LLM prompt.

A concrete pattern (illustrative — no code shipped here):

1. During a session, **stage** candidate secrets the sanitizer flagged into a local, permission-
   locked staging file (never committed).
2. At the end, **review** the staged list (values masked) and, only after explicit human approval,
   **commit** them to your vault of choice — e.g. 1Password, HashiCorp Vault, or any secret store.
3. The vault returns a **reference** (for 1Password, an `op://<vault>/<item>/<field>` URI); store
   only that reference in memory. Retrieval later resolves the reference through the vault (e.g.
   with a per-item unlock / Touch ID prompt).

So the two halves compose: the **sanitizer** scrubs secrets out of text on the way in
(`SANITIZER_URL`), and a small **vault CLI** of your choosing captures the scrubbed-out values and
hands memory back only a `…`-style reference. Build the vault side to fit your own secret store;
this repo intentionally ships only the sanitizer integration and leaves the vault choice to you.

## Hardening (beyond the tailnet)

The MCP server has no built-in auth. A ready-made bearer-token layer ships as
`Caddyfile.auth.example`: copy it to `Caddyfile.auth`, set `CADDY_CONFIG=./Caddyfile.auth` and
`MCP_TOKEN` in `.env`, recreate the caddy service, and add the
`Authorization: Bearer <MCP_TOKEN>` header to every MCP client. For anything beyond that, put a
reverse proxy with OIDC (e.g. Authelia) in front, keep image tags pinned, and upgrade the
"experimental" MCP server deliberately.
