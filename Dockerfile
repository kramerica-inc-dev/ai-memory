# Custom graphiti-mcp image.
#
# Attribution / upstream:
#   - Base image: zepai/knowledge-graph-mcp (the standalone Graphiti MCP server image),
#     pinned here to the graphiti-0.28.2 build.
#   - Upstream project: Graphiti — https://github.com/getzep/graphiti (the temporal
#     knowledge-graph engine and MCP server this whole stack is built on).
#
# Everything below is plain COMPATIBILITY glue for that specific image/version — nothing secret:
#   1) the bare standalone image lacks `google-genai`, so graphiti-core's native Gemini clients
#      (LLM, embedder, reranker) will not load. We add it so provider "gemini" works natively —
#      no OpenAI-compat shim needed. `anthropic` is added so Claude can be an extraction LLM.
#   2) three small version-specific patches (see the RUN steps + patch-anthropic-client.py) work
#      around behavior of graphiti-core 0.28.2 against current provider APIs. Revisit/remove them
#      when you bump the base image.
FROM zepai/knowledge-graph-mcp:1.0.2-graphiti-0.28.2-standalone

# google-genai for the (native) Gemini embedder; anthropic for Claude as the extraction LLM.
RUN /root/.local/bin/uv pip install --python /app/mcp/.venv/bin/python --no-cache "google-genai==2.10.0" "anthropic"

# graphiti-core 0.28.2 always sends `temperature` to Anthropic; some newer Claude models have
# dropped that parameter (400). Patch: drop temperature -> Anthropic uses its default.
RUN sed -i '/temperature=self.temperature,/d' \
    /app/mcp/.venv/lib/python3.11/site-packages/graphiti_core/llm_client/anthropic_client.py

# graphiti-core 0.28.2 fetches structured output with plain forced tool-use (no `strict`),
# which on some newer Claude models intermittently drops episodes via malformed tool_use.input
# ({} or a $PARAMETER_VALUE wrapper -> Pydantic validation error). Patch: use messages.parse()
# (output_config.format, schema-constrained). See patch-anthropic-client.py + ARCHITECTURE.md.
COPY patch-anthropic-client.py /tmp/patch-anthropic-client.py
RUN /app/mcp/.venv/bin/python /tmp/patch-anthropic-client.py

# Bound each Anthropic request (env LLM_REQUEST_TIMEOUT, default 200s): the client is created with
# no timeout (600s SDK default), so a hung extraction call stalls a whole add_episode_bulk batch.
# See patch-anthropic-timeout.py.
COPY patch-anthropic-timeout.py /tmp/patch-anthropic-timeout.py
RUN /app/mcp/.venv/bin/python /tmp/patch-anthropic-timeout.py

# The MCP factory gives gpt-5 models reasoning='minimal', but some gpt-5 variants reject that
# (only none/low/medium/high -> 400). Set to 'low' (valid across the family). Harmless for
# non-gpt-5 models. Adjust or remove for your provider if not applicable.
RUN sed -i "s/reasoning='minimal'/reasoning='low'/" /app/mcp/src/services/factories.py

# DURABLE QUEUE (replaces the stock in-memory queue service) — fixes three incident
# classes at once: restart-drops (Redis Streams, AOF-durable), rate-limit drops (real
# backoff + dead-letter stream), and cross-group graph corruption from graphiti-core
# 0.28.2's shared-driver mutation (single strictly-serialized consumer). See
# queue_service_durable.py. For a minimal alternative without the durable queue,
# patch-queue-lock.py (in this repo) provides just the corruption fix.
COPY queue_service_durable.py /app/mcp/src/services/queue_service.py
COPY patch-durable-queue.py /tmp/patch-durable-queue.py
RUN /app/mcp/.venv/bin/python /tmp/patch-durable-queue.py

# EdgeDuplicate's required fields drop an entire episode when one structured-output call
# comes back malformed (~25% observed during a bulk re-ingest on the Anthropic path).
# Patch: safe list defaults. See patch-dedupe-defaults.py.
COPY patch-dedupe-defaults.py /tmp/patch-dedupe-defaults.py
RUN /app/mcp/.venv/bin/python /tmp/patch-dedupe-defaults.py

# FALKORDB FULLTEXT HARDENING (failure class: "RediSearch: Syntax error ..."):
# group_id escaping (backport of upstream PR #1549), empty/stopword-only guard
# (upstream #1337/#1440) and backtick separator (#1440), applied to BOTH duplicated
# fulltext builders. See the patch files' docstrings.
COPY patch-falkor-fulltext-escape.py /tmp/patch-falkor-fulltext-escape.py
RUN /app/mcp/.venv/bin/python /tmp/patch-falkor-fulltext-escape.py
COPY patch-falkor-fulltext-escape-driver.py /tmp/patch-falkor-fulltext-escape-driver.py
RUN /app/mcp/.venv/bin/python /tmp/patch-falkor-fulltext-escape-driver.py

# EMPTY GRAPH NAME GUARD (failure class: "Expected a string parameter, but received
# <class 'str'>"): an empty group_id must never reach falkordb-py's select_graph.
# Upstream: getzep/graphiti#1650 + FalkorDB/falkordb-py#244 (both reported by us).
COPY patch-falkor-empty-graphname.py /tmp/patch-falkor-empty-graphname.py
RUN /app/mcp/.venv/bin/python /tmp/patch-falkor-empty-graphname.py

# EDGE FULLTEXT PERF (failure class: "Query timed out" on dense graphs): stop
# re-MATCHing each fulltext hit by uuid (a per-hit label scan over all Entity nodes);
# consume the hit via startNode()/endNode(). Upstream getzep/graphiti#1272. Patched in
# the live generic path (search_utils.py) AND the dormant falkordb search-ops path.
COPY patch-falkor-edge-fulltext-scan.py /tmp/patch-falkor-edge-fulltext-scan.py
RUN /app/mcp/.venv/bin/python /tmp/patch-falkor-edge-fulltext-scan.py
COPY patch-falkor-edge-fulltext-scan-searchutils.py /tmp/patch-falkor-edge-fulltext-scan-searchutils.py
RUN /app/mcp/.venv/bin/python /tmp/patch-falkor-edge-fulltext-scan-searchutils.py

# Optional LOCAL reranker via the infinity container's /rerank endpoint — removes the
# unconditional OpenAI dependency of graphiti-core's default cross-encoder. Opt-in:
# active only when RERANKER_URL is set (see docker-compose.yml + patch-reranker.py).
COPY infinity_reranker_client.py /app/mcp/src/infinity_reranker_client.py
COPY patch-reranker.py /tmp/patch-reranker.py
RUN /app/mcp/.venv/bin/python /tmp/patch-reranker.py
