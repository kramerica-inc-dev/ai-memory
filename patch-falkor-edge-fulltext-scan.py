#!/usr/bin/env python3
"""
Build-time patch: edge fulltext search — use the fulltext hit directly (perf).

graphiti-core 0.28.2's FalkorDB edge_fulltext_search yields each fulltext hit as a
relationship (`YIELD relationship AS rel, score`) and then throws it away, re-MATCHing
the same edge by uuid: `MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)`.
GRAPH.EXPLAIN shows the re-MATCH resolved per hit via a label scan over all Entity
nodes driving an edge-index scan (Node By Label Scan + Edge By Index Scan) ->
O(hits x nodes). On our trading graph this query shape made routine edge searches
exceed even the 300-600s FalkorDB query timeouts on an idle machine, parking episodes
in the dead-letter queue with "Query timed out".

NOTE: in 0.28.2 this FalkorSearchOperations path is dormant (GraphDriver.
search_interface is never assigned); the query actually executed at runtime is the
identical pattern in search_utils.py, fixed by the sibling patch
patch-falkor-edge-fulltext-scan-searchutils.py. This file is patched for consistency,
so the defect does not resurface if a later version activates search_interface.

Fix (upstream open issue getzep/graphiti#1272): consume the hit itself — bind
`rel AS e` and derive endpoints via `startNode(rel)` / `endNode(rel)`. Same bindings
(e, score, n, m) flow into the unchanged WHERE/WITH/RETURN/ORDER/LIMIT structure, so
filters (incl. e.group_id IN $group_ids) and downstream record parsing are unaffected.
RELATES_TO edges connect Entity nodes by construction, so dropping the label re-check
does not change semantics.

Idempotent; fails the build loudly if the upstream code no longer matches. The marker
is patch-specific ("getzep/graphiti#1272") because this file also carries the fulltext
escaping patch in the same build.
"""
import sys

PATH = "/app/mcp/.venv/lib/python3.11/site-packages/graphiti_core/driver/falkordb/operations/search_ops.py"

OLD = (
    '            + """\n'
    "            YIELD relationship AS rel, score\n"
    "            MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)\n"
    '            """\n'
)

NEW = (
    "            # PATCH (ai-memory): consume the fulltext hit directly via startNode()/\n"
    "            # endNode() instead of re-MATCHing the edge by uuid, which scanned every\n"
    "            # RELATES_TO edge per hit (O(hits x edges)); upstream getzep/graphiti#1272.\n"
    '            + """\n'
    "            YIELD relationship AS rel, score\n"
    "            WITH rel AS e, score, startNode(rel) AS n, endNode(rel) AS m\n"
    '            """\n'
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "getzep/graphiti#1272" in src:
        print("search_ops.py: edge-fulltext scan fix already patched")
        return 0
    n = src.count(OLD)
    if n != 1:
        print(f"search_ops.py: expected 1 edge-fulltext re-MATCH fragment, found {n} — aborting",
              file=sys.stderr)
        return 1
    open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("search_ops.py: edge-fulltext re-MATCH replaced with startNode()/endNode()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
