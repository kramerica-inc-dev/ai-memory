#!/usr/bin/env python3
"""
Build-time patch: never pass a falsy graph name to falkordb-py's select_graph().

Incident 2026-07-13: episodes dead-lettered with "Expected a string parameter, but
received <class 'str'>". Root cause (repro'd 2026-07-14): a malformed queue entry
carried no group_id field, the consumer defaults that to '' (f.get('group_id', '')),
and add_episode ran FalkorDriver.clone(database=''). clone() special-cases
self._database and default_group_id ('\\_'), but '' matches neither, so it built a
FalkorDriver whose graph name is '' — that construction SUCCEEDS (select_graph is
lazy), and the first query then hits falkordb-py's select_graph(''), which rejects ''
inside `if not isinstance(graph_id, str) or graph_id == "":`, raising the misleading
isinstance-style message above. graphiti-core 0.28.2 has no guard anywhere on this
path (validate_group_id explicitly allows '').

Reported upstream by us on 2026-07-14: getzep/graphiti#1650 (empty group_id ->
clone('') -> crash) and FalkorDB/falkordb-py#244 (misleading error message). No
released version fixes either; hence this build-time guard.

Two fragments (defense in depth):
1. clone(): falsy `database` falls back to self._database before the existing
   special-case chain, so clone('') returns the current driver instead of crashing.
2. _get_graph(): treat '' (any falsy name) like None and fall back to self._database.

This patch runs AFTER patch-falkor-fulltext-escape-driver.py in the same build; both
fragments below sit outside the regions that patch touches (sanitize map and
build_fulltext_query).

Idempotent; fails the build loudly if the upstream code no longer matches.
"""
import sys

PATH = "/app/mcp/.venv/lib/python3.11/site-packages/graphiti_core/driver/falkordb_driver.py"

# Fragment 1: clone() — the branch chain at lines 312-315 in graphiti-core 0.28.2.
OLD_CLONE = (
    "        if database == self._database:\n"
    "            cloned = self\n"
    "        elif database == self.default_group_id:\n"
    "            cloned = FalkorDriver(falkor_db=self.client)\n"
)
NEW_CLONE = (
    "        # PATCH (ai-memory): a falsy database name (e.g. '') must never reach\n"
    "        # falkordb-py's select_graph. On 2026-07-13 an empty group_id flowed into\n"
    "        # clone('') and crashed with the misleading \"Expected a string parameter,\n"
    "        # but received <class 'str'>\" (select_graph('') fails its combined\n"
    "        # isinstance/empty check). Reported upstream by us: getzep/graphiti#1650\n"
    "        # and FalkorDB/falkordb-py#244. Fall back to this driver's default database.\n"
    "        if not database:\n"
    "            database = self._database\n"
    "        if database == self._database:\n"
    "            cloned = self\n"
    "        elif database == self.default_group_id:\n"
    "            cloned = FalkorDriver(falkor_db=self.client)\n"
)

# Fragment 2: _get_graph() — lines 218-221 in graphiti-core 0.28.2.
OLD_GET_GRAPH = (
    '        # FalkorDB requires a non-None database name for multi-tenant graphs; the default is "default_db"\n'
    "        if graph_name is None:\n"
    "            graph_name = self._database\n"
    "        return self.client.select_graph(graph_name)\n"
)
NEW_GET_GRAPH = (
    '        # FalkorDB requires a non-None database name for multi-tenant graphs; the default is "default_db"\n'
    "        # PATCH (ai-memory): treat '' (any falsy name) like None — falkordb-py's\n"
    "        # select_graph('') crashes with a misleading TypeError (incident 2026-07-13;\n"
    "        # upstream getzep/graphiti#1650, FalkorDB/falkordb-py#244). Defense in depth\n"
    "        # next to the same guard in clone().\n"
    "        if not graph_name:\n"
    "            graph_name = self._database\n"
    "        return self.client.select_graph(graph_name)\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "getzep/graphiti#1650" in src:
        print("falkordb_driver.py: empty-graph-name guards already patched")
        return 0
    for frag, label in ((OLD_CLONE, "clone-guard"), (OLD_GET_GRAPH, "_get_graph-guard")):
        n = src.count(frag)
        if n != 1:
            print(
                f"falkordb_driver.py: expected 1 match for {label}, found {n} — aborting",
                file=sys.stderr,
            )
            return 1
    src = src.replace(OLD_CLONE, NEW_CLONE).replace(OLD_GET_GRAPH, NEW_GET_GRAPH)
    open(PATH, "w", encoding="utf-8").write(src)
    print("falkordb_driver.py: empty-graph-name guards inserted (clone + _get_graph)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
