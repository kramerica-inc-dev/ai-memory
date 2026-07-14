#!/usr/bin/env python3
"""
Build-time patch: edge fulltext search (generic path) — use the fulltext hit directly.

Mirror of patch-falkor-edge-fulltext-scan.py for the provider-generic
edge_fulltext_search in graphiti_core/search/search_utils.py. That function builds the
pathological pattern `YIELD relationship AS rel, score` followed by
`MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)`, which re-matches the
already-yielded edge by uuid. GRAPH.EXPLAIN shows that re-MATCH resolved per hit via a
label scan over all Entity nodes driving an edge-index scan (Node By Label Scan + Edge
By Index Scan) -> O(hits x nodes) (upstream open issue getzep/graphiti#1272). In
0.28.2 THIS is the path actually executed at runtime (GraphDriver.search_interface is
never assigned, so the FalkorSearchOperations variant is dormant) — this patch is the
operative bug-A fix. On our trading graph this query shape blew past the 300-600s
FalkorDB timeouts on an idle machine ("Query timed out" dead-letters).

Fix: bind `rel AS e` and derive endpoints via `startNode(rel)` / `endNode(rel)` — same
bindings (e, score, n, m) feed the unchanged filter/WITH/RETURN/ORDER/LIMIT structure.
startNode()/endNode() are standard Cypher functions on both FalkorDB and Neo4j (the two
providers that use this match_query; Kuzu overrides it and Neptune takes its own
branch), and RELATES_TO edges connect Entity nodes by construction, so semantics are
preserved. Only the default `match_query` assignment is touched; score and limit
handling stay untouched.

Idempotent; fails the build loudly if the upstream code no longer matches. The marker
is patch-specific ("getzep/graphiti#1272"); note the file already contains
startNode()/endNode() in its Neptune branch, so those are not usable as markers.
"""
import sys

PATH = "/app/mcp/.venv/lib/python3.11/site-packages/graphiti_core/search/search_utils.py"

OLD = (
    '    match_query = """\n'
    "    YIELD relationship AS rel, score\n"
    "    MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)\n"
    '    """\n'
)

NEW = (
    "    # PATCH (ai-memory): consume the fulltext hit directly via startNode()/endNode()\n"
    "    # instead of re-MATCHing the edge by uuid, which scanned every RELATES_TO edge\n"
    "    # per hit (O(hits x edges)); upstream getzep/graphiti#1272.\n"
    '    match_query = """\n'
    "    YIELD relationship AS rel, score\n"
    "    WITH rel AS e, score, startNode(rel) AS n, endNode(rel) AS m\n"
    '    """\n'
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "getzep/graphiti#1272" in src:
        print("search_utils.py: edge-fulltext scan fix already patched")
        return 0
    n = src.count(OLD)
    if n != 1:
        print(f"search_utils.py: expected 1 edge-fulltext re-MATCH fragment, found {n} — aborting",
              file=sys.stderr)
        return 1
    open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("search_utils.py: edge-fulltext re-MATCH replaced with startNode()/endNode()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
