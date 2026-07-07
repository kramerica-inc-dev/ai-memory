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

# The MCP factory gives gpt-5 models reasoning='minimal', but some gpt-5 variants reject that
# (only none/low/medium/high -> 400). Set to 'low' (valid across the family). Harmless for
# non-gpt-5 models. Adjust or remove for your provider if not applicable.
RUN sed -i "s/reasoning='minimal'/reasoning='low'/" /app/mcp/src/services/factories.py

# CRITICAL — graphiti-core 0.28.2 mutates the shared driver per group_id (graphiti.py:889);
# concurrent group workers write into each other's graph (cross-group corruption: episodes
# land in the wrong graph carrying the right group_id property). Patch: a global episode
# lock in the queue service serializes processing across groups. See patch-queue-lock.py.
COPY patch-queue-lock.py /tmp/patch-queue-lock.py
RUN /app/mcp/.venv/bin/python /tmp/patch-queue-lock.py

# EdgeDuplicate's required fields drop an entire episode when one structured-output call
# comes back malformed (~25% observed during a bulk re-ingest on the Anthropic path).
# Patch: safe list defaults. See patch-dedupe-defaults.py.
COPY patch-dedupe-defaults.py /tmp/patch-dedupe-defaults.py
RUN /app/mcp/.venv/bin/python /tmp/patch-dedupe-defaults.py
