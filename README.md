# ai-memory

A self-hosted, **shared temporal knowledge graph** that acts as long-term memory for all your
MCP clients (Claude Code/Desktop, Cursor, Windsurf, Cline). One store, reachable over a private
network, so a fact you save in one tool is readable in another.

Built on [Graphiti](https://github.com/getzep/graphiti) (temporal knowledge graph) + FalkorDB
(graph database), exposed over MCP. The whole extraction/embedding/reranking pipeline is
**provider-agnostic**: swap the LLM, the embedder, or the reranker with an env change — no code
edits — via a small control plane (`memctl.sh` + `profiles/`).

> **Content-agnostic by design.** This repository is the *tool* only. Your namespaces, directory
> routing, hosts and secrets live in local config (`config/mapping.yaml`, `.env`, `memctl.conf`)
> that is gitignored. Nothing about your content is baked into the code.

## Architecture

```
                       private tailnet (e.g. Tailscale/WireGuard)
  MCP clients  ───────────────  HTTP/MCP  ───────────────►  Caddy :8000
  (Claude Code, Cursor,                                       │ rewrites Host -> localhost
   Windsurf, Cline, Desktop)                                  ▼
                                                          graphiti-mcp  ──►  FalkorDB
                                                          (MCP server)       (graph + vectors,
                                                               │              internal only)
                                     three pluggable provider layers:
                                       1. extraction LLM   (OpenAI / Anthropic / Gemini / Ollama)
                                       2. embedder         (Gemini / OpenAI-compat: infinity, Ollama)
                                       3. reranker         (OpenAI-compatible logprobs endpoint)

  ingest (two routes):
    bulk (onboarding)      files/notes ─► sanitizer ─► chunk ─► add_episode_bulk   (bulk_ingest.py)
    add_memory (daily)     client/agent ─► sanitizer ─► add_memory (per-episode dedup)
```

- **`group_id` = namespace = a separate graph.** Clients pass a `group_id` per call so memory
  does not leak across areas. Your namespaces and how directories/projects map into them live in
  `config/mapping.yaml` (copy from `config/mapping.example.yaml`).
- **Hygiene gate.** Ingest paths send text through a secret-sanitizer first (fail-closed), so
  secrets/PII never reach the extraction LLM or the graph. The tool expects a simple
  `POST /api/sanitize` service; a ready-made one is
  [secret-sanitizer](https://github.com/kramerica-inc-dev/secret-sanitizer) — set `SANITIZER_URL`
  to point at it (or bring your own). See `sanitize_ingest.py` and `docs/DEPLOY.md`.
- **Model names are examples.** The `gpt-5.4-mini` / `claude-sonnet-5` / `gemini-2.5-flash` /
  `bge-m3` defaults are just presets — you pick your own provider + model per profile/env. See
  "Choosing your models" in `docs/DEPLOY.md`.

## Quickstart

Requirements: Docker + Docker Compose. An API key for at least one LLM provider (and an embedder).

```bash
cp .env.example .env                        # fill in GOOGLE_API_KEY / OPENAI_API_KEY + FALKORDB_PASSWORD
cp config/mapping.example.yaml config/mapping.yaml   # define your namespaces + routing
docker compose up -d                        # builds the custom image, starts FalkorDB + MCP + Caddy

curl -sS http://localhost:8000/mcp/ -H 'Accept: text/event-stream' -i | head   # healthcheck (SSE 200)
```

Then point a client at `http://<host>:8000/mcp` — see [`clients/README.md`](clients/README.md).

### Switching providers (no code edits)

```bash
cp memctl.conf.example memctl.conf          # set MEMORY_HOST/MEMORY_DIR/MEMORY_GROUPS
./memctl.sh doctor                          # verify live provider config + key presence
./memctl.sh switch claude                   # hot-swap the extraction LLM (embedder unchanged)
./memctl.sh reindex --to local-embedder     # guarded, destructive embedder swap (wipe + re-embed)
```

Profiles live in `profiles/*.env` (openai-cloud, gemini, claude, haiku, local-embedder,
local-embedder-remote, local-ollama). A **switch** is a cheap LLM-only swap; changing the
**embedder** model/dimension invalidates stored vectors and must go through **reindex**.

## The ingest routes

- **Serial mass-ingest (rebuild/backfill)** — `./memctl.sh enqueue <chunks.jsonl>` feeds a JSONL
  dump onto the durable queue; the live consumer drains it one episode at a time through the exact
  daily-use path (sanitizer, full per-episode dedup, bounded retry, dead-letter). Slower than bulk
  but batch-failure-proof — the proven route for rebuilds and dense documents.
- **Bulk (onboarding)** — read an existing library of files/notes in one pass.
  `migrate/index_files.py --dump` (or `migrate/backfill.py`) produces JSONL; `./memctl.sh ingest`
  runs `add_episode_bulk` inside the container. Far fewer LLM/embed/DB calls than per-episode.
  Deliberately no in-batch retry: `add_episode_bulk` can partially land episodes before raising,
  so a retry would duplicate them — failed batches dead-letter instead, for serial replay.
- **`add_memory` (daily)** — incremental, full per-episode dedup. This is what MCP clients call
  during normal use, and what `backfill.py`/`index_files.py` use without `--dump`.

In-place correction, no rebuilds: `./memctl.sh dedup --groups <g> [--apply]` removes duplicate
episodes (same name + source_description) via graphiti's own `remove_episode`, so facts shared
with surviving episodes stay intact (dry-run by default). `./memctl.sh dead-letter
--list|--replay|--purge` inspects and idempotently replays quarantined episodes.

All routes share per-group idempotency sets (`aimem:processed:<group>`), so re-submitting a landed
episode is a no-op and `./memctl.sh wipe --groups <g>` stays surgical: it clears only those groups'
graphs, processed-keys and queue/dead entries — never another namespace's state. Deployments coming
from the older single global set migrate once with `./memctl.sh split-processed` (idempotent,
dry-run by default; see the tool's docstring for the cutover order).

Optional file layers: `migrate/mirror.sh` mirrors your projects one-way to the host (so search
results can point back at real files), and `migrate/index_files.py` indexes their content into the
graph. `migrate/verify_index.py` checks that queued chunks actually landed.

## Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the layers and the design choices behind them.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — deploy + configuration, embedder options, hardening.
- [`clients/README.md`](clients/README.md) — wiring up each MCP client.

## Security notes

- **No public port.** Port 8000 is meant for a private tailnet. The Graphiti MCP server has no
  built-in auth — do not expose it publicly. To go beyond the tailnet, put a reverse proxy with
  bearer/OIDC auth in front (see `docs/DEPLOY.md`).
- FalkorDB and the Browser UI are **not** published to the host (internal compose network only).
- The Graphiti MCP server is officially "experimental" — pin image tags, upgrade deliberately.
- **Secrets never enter the graph.** Detected secrets go to a dedicated secret manager; only an
  opaque reference is stored in memory. See "Secret management" in `docs/DEPLOY.md` for the pattern.

## Credits

Built on the shoulders of these open-source projects:

- **[Graphiti](https://github.com/getzep/graphiti)** — the temporal knowledge-graph engine (episodes, entities, facts) this project wraps and operates.
- **[FalkorDB](https://github.com/FalkorDB/FalkorDB)** — the graph database backing the store.
- **[Infinity](https://github.com/michaelf34/infinity)** — the OpenAI-compatible embeddings server, running **[BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3)** as the vendor-free embedding model.
- **[secret-sanitizer](https://github.com/kramerica-inc-dev/secret-sanitizer)** — the hygiene gate that strips secrets/PII before ingest (itself built on Betterleaks/Gitleaks, Presidio and Deduce).
- **[Caddy](https://github.com/caddyserver/caddy)** — the reverse proxy in front of the MCP endpoint.
- **[Model Context Protocol](https://github.com/modelcontextprotocol)** — the client-agnostic protocol every MCP client speaks.

## License

MIT — see [`LICENSE`](LICENSE).
