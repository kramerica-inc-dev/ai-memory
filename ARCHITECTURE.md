# Architecture

The tool is a thin, provider-agnostic control layer around Graphiti + FalkorDB, exposed over MCP.
This document describes the layers and the one-line rationale behind each design choice.

## Upstream & attribution

This project stands on:

- **Graphiti** — [github.com/getzep/graphiti](https://github.com/getzep/graphiti) — the temporal
  knowledge-graph engine and MCP server. It does the heavy lifting (episodes, entities, temporal
  facts, dedup, search); this repo is deployment glue + a provider control plane around it.
- **`zepai/knowledge-graph-mcp`** — the standalone Graphiti MCP server container image, pinned
  here to the `graphiti-0.28.2` build (see `Dockerfile`).
- **FalkorDB**, **Caddy**, and (optionally) **infinity** (bge-m3 embeddings) as the surrounding
  services.

The `Dockerfile` adds native provider SDKs and three small **version-specific compatibility
patches** (see below and `patch-anthropic-client.py`). Nothing there is secret — it is purely glue
to make graphiti-core 0.28.2 behave against current provider APIs; revisit it when you bump the
base image.

## Layers

```
MCP clients ─HTTP/MCP─► Caddy :8000 ─► graphiti-mcp ─► FalkorDB
                          (Host rewrite)   (MCP server)    (graph + vector index)
                                              │
                     ┌────────────────────────┼────────────────────────┐
                     ▼                         ▼                        ▼
              extraction LLM             embedder                 reranker
        (OpenAI/Anthropic/Gemini/    (Gemini or OpenAI-compat:   (OpenAI-compatible
         Ollama via gateway)          infinity/bge-m3, Ollama)    logprobs endpoint)
```

- **Caddy** is the only published port. It rewrites the `Host` header to `localhost` and strips
  `Origin` so MCP clients on a tailnet do not trip FastMCP's DNS-rebinding protection.
- **graphiti-mcp** is a locally built image: a standalone knowledge-graph-mcp base + native
  provider SDKs (`google-genai`, `anthropic`) + a couple of build-time patches (see below).
- **FalkorDB** holds one graph per `group_id`, including the vector index for semantic search.

## Design choices (one-line rationale)

- **FalkorDB as the graph DB** — a single Redis-module process gives graph + vector search with a
  trivial deploy (one container, RDB snapshot persistence); no separate vector store to run.
- **Graphiti for temporal memory** — facts carry validity time and are *superseded* rather than
  overwritten, so "what changed / what was true then" is answerable; new info invalidates old.
- **Provider-agnostic via env, not code** — `config.yaml` expands `${VAR:default}` on the
  `provider:` string itself, so `profiles/*.env` + a restart swaps providers with no config edit.
- **Three independent provider layers** — extraction LLM, embedder, and reranker are chosen
  separately, so you can (e.g.) run local embeddings while keeping a cloud extraction LLM.
- **`memctl switch` vs `memctl reindex`** — an LLM swap is cheap and hot (vectors stay valid); an
  embedder model/dimension change invalidates every stored vector, so it is a separate, guarded,
  destructive reindex. `switch` refuses embedder changes to prevent silent 0-result search.
- **Dimension consistency guard** — `EMBEDDER_DIMENSIONS` must equal `EMBEDDING_DIM`; otherwise
  vector search breaks after a FalkorDB restart. `memctl doctor` asserts this on the live env.
- **Three ingest routes** — bulk (`add_episode_bulk`) dedups across a whole batch in one pass for
  cheap onboarding; serial (`memctl enqueue` → durable queue) trades speed for per-episode
  isolation, the route for rebuilds and dense content; per-episode (`add_memory`) does full dedup
  for accurate daily writes. Bulk deliberately has NO in-batch retry: a batch can partially land
  before an exception, so re-submitting would duplicate episodes — failures dead-letter instead,
  and `memctl dedup` corrects duplicate episodes in place (via `remove_episode`, shared facts
  survive) so a corrupted graph never needs a from-scratch rebuild.
- **Sanitizer hygiene gate** — all ingest paths scrub text through a secret-sanitizer *before* it
  reaches the LLM or the graph, fail-closed (no sanitizer reachable = no ingest), so credentials
  and PII never leak into memory. Detected secrets belong in your own secret manager, not the graph.
- **Idempotent ingest with ledgers** — `backfill.py` (a line ledger) and `index_files.py` (a
  `path -> sha256` ledger) make re-runs cheap and duplicate-free; `verify_index.py` reconciles the
  ledger against what actually landed in FalkorDB (the queue is in-memory and can drop on restart).
- **Content lives in config, not code** — namespaces and directory/project routing come from
  `config/mapping.yaml`; the scripts fail closed if it is missing rather than assuming a taxonomy.

## Build-time patches (Dockerfile)

These adapt a pinned Graphiti version to current provider behavior. They are optional depending on
your provider; keep, adjust, or drop them:

- **Drop `temperature` for Anthropic** — some newer Claude models reject the parameter (400).
- **Structured output via `messages.parse()`** — plain forced tool-use intermittently emitted a
  malformed `tool_use.input` on some Claude models, dropping episodes; `parse()` is schema-
  constrained. Falls back to the original path on error. See `patch-anthropic-client.py`.
- **`reasoning='low'` for gpt-5-family** — some variants reject `'minimal'` (400).

## Durability notes

- Data survives a reboot (FalkorDB RDB snapshot). But FalkorDB does not persist the vector index in
  RDB, and Graphiti rebuilds only the default group's index at startup — so right after a reboot,
  semantic search on a given group can return 0 results until the first write to that group rebuilds
  its index (self-heal). A warm-up `add_memory` per active group avoids the gap.
- The FalkorDB image's default `TIMEOUT 1000` is a hard 1s read-query limit that Graphiti's dedup
  fulltext queries outgrow as the graph grows; the compose file replaces it with
  `TIMEOUT_DEFAULT`/`TIMEOUT_MAX`. See `docs/DEPLOY.md`.
