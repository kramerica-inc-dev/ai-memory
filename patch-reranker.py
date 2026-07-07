#!/usr/bin/env python3
"""
Build-time patch: optional local reranker (infinity) instead of the OpenAI default.

graphiti-core's Graphiti() falls back to OpenAIRerankerClient when no cross_encoder
is passed, and the MCP server never passes one — making search-ranking an
unconditional OpenAI dependency regardless of the configured LLM/embedder.

This patch wires InfinityRerankerClient (see infinity_reranker_client.py) into both
Graphiti() instantiation sites, gated on the RERANKER_URL env var: unset → stock
OpenAI behavior, set → local reranking via the infinity container.

Idempotent; fails the build loudly if upstream code no longer matches.
"""
import sys

PATH = "/app/mcp/src/graphiti_mcp_server.py"

ANCHOR = (
    "            # Initialize Graphiti client with appropriate driver\n"
    "            try:\n"
)
ANCHOR_NEW = (
    "            # Initialize Graphiti client with appropriate driver\n"
    "            try:\n"
    "                # PATCH (ai-memory): optional local reranker — active only when\n"
    "                # RERANKER_URL is set; otherwise graphiti-core's OpenAI default applies.\n"
    "                _xenc = None\n"
    "                if os.environ.get('RERANKER_URL'):\n"
    "                    from infinity_reranker_client import InfinityRerankerClient\n"
    "                    _xenc = InfinityRerankerClient()\n"
)

CALL_FALKOR = (
    "                    self.client = Graphiti(\n"
    "                        graph_driver=falkor_driver,\n"
    "                        llm_client=llm_client,\n"
    "                        embedder=embedder_client,\n"
    "                        max_coroutines=self.semaphore_limit,\n"
    "                    )\n"
)
CALL_FALKOR_NEW = (
    "                    self.client = Graphiti(\n"
    "                        graph_driver=falkor_driver,\n"
    "                        llm_client=llm_client,\n"
    "                        embedder=embedder_client,\n"
    "                        cross_encoder=_xenc,\n"
    "                        max_coroutines=self.semaphore_limit,\n"
    "                    )\n"
)

CALL_NEO4J = (
    "                    self.client = Graphiti(\n"
    "                        uri=db_config['uri'],\n"
    "                        user=db_config['user'],\n"
    "                        password=db_config['password'],\n"
    "                        llm_client=llm_client,\n"
    "                        embedder=embedder_client,\n"
    "                        max_coroutines=self.semaphore_limit,\n"
    "                    )\n"
)
CALL_NEO4J_NEW = (
    "                    self.client = Graphiti(\n"
    "                        uri=db_config['uri'],\n"
    "                        user=db_config['user'],\n"
    "                        password=db_config['password'],\n"
    "                        llm_client=llm_client,\n"
    "                        embedder=embedder_client,\n"
    "                        cross_encoder=_xenc,\n"
    "                        max_coroutines=self.semaphore_limit,\n"
    "                    )\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "PATCH (ai-memory): optional local reranker" in src:
        print("graphiti_mcp_server.py: reranker al gepatcht")
        return 0
    for frag, label in ((ANCHOR, "init-anchor"), (CALL_FALKOR, "falkordb-call"), (CALL_NEO4J, "neo4j-call")):
        n = src.count(frag)
        if n != 1:
            print(f"graphiti_mcp_server.py: verwachtte 1 match voor {label}, vond {n} — afgebroken",
                  file=sys.stderr)
            return 1
    src = src.replace(ANCHOR, ANCHOR_NEW).replace(CALL_FALKOR, CALL_FALKOR_NEW).replace(CALL_NEO4J, CALL_NEO4J_NEW)
    open(PATH, "w", encoding="utf-8").write(src)
    print("graphiti_mcp_server.py: lokale-reranker-wiring ingevoegd (env-gated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
