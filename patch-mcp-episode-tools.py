#!/usr/bin/env python3
"""
Build-time patch: make the MCP episode tools group-graph-aware on FalkorDB.

On FalkorDB every group_id is its own graph, but the MCP server's get_episodes and
delete_episode operate on the shared driver as-is — bound to default_db at startup,
or to the last-written group after any add_episode (which rebinds the driver). Both
tools therefore query the wrong graph and report existing data as missing:
get_episodes(group_ids=['x']) returns "No episodes found" while the episode is
demonstrably present in the 'x' graph, and delete_episode(uuid) fails with
"node ... not found" (observed 2026-07-14 during warmup cleanup).

Reported upstream by us as getzep/graphiti#1651 (the handlers bypass the
handle_multiple_group_ids decorator, so the pending decorator fixes do not cover
them). This patch is the deployment-side fix until upstream lands one:

1. get_episodes: clone the driver per requested group and query that group's graph.
2. delete_episode: accept an optional group_id; when absent, scan all graphs for the
   uuid (cheap: one indexed lookup per graph) before deleting via the right driver.

FalkorDB-specific by design — this deployment runs FalkorDB only.

Idempotent; fails the build loudly if the upstream code no longer matches.
"""
import sys

PATH = "/app/mcp/src/graphiti_mcp_server.py"

MARKER = "getzep/graphiti#1651"

OLD_DELETE_SIG = (
    "async def delete_episode(uuid: str) -> SuccessResponse | ErrorResponse:\n"
    '    """Delete an episode from the graph memory.\n'
    "\n"
    "    Args:\n"
    "        uuid: UUID of the episode to delete\n"
    '    """\n'
)
NEW_DELETE_SIG = (
    "async def delete_episode(\n"
    "    uuid: str, group_id: str | None = None\n"
    ") -> SuccessResponse | ErrorResponse:\n"
    '    """Delete an episode from the graph memory.\n'
    "\n"
    "    Args:\n"
    "        uuid: UUID of the episode to delete\n"
    "        group_id: Optional group whose graph holds the episode (FalkorDB keeps one\n"
    "                  graph per group; without it, all graphs are searched for the uuid)\n"
    '    """\n'
)

OLD_DELETE_BODY = (
    "        # Get the episodic node by UUID\n"
    "        episodic_node = await EpisodicNode.get_by_uuid(client.driver, uuid)\n"
    "        # Delete the node using its delete method\n"
    "        await episodic_node.delete(client.driver)\n"
    "        return SuccessResponse(message=f'Episode with UUID {uuid} deleted successfully')\n"
)
NEW_DELETE_BODY = (
    "        # PATCH (ai-memory): on FalkorDB each group_id is its own graph, so a uuid\n"
    "        # lookup on the shared driver (bound to default_db or the last-written\n"
    "        # group) fails with 'node not found'. Look in the given group's graph, or\n"
    "        # scan all graphs for the uuid. Upstream: getzep/graphiti#1651.\n"
    "        if group_id:\n"
    "            candidate_graphs = [group_id]\n"
    "        else:\n"
    "            candidate_graphs = await client.driver.client.list_graphs()\n"
    "        episodic_node = None\n"
    "        found_driver = None\n"
    "        for graph_name in candidate_graphs:\n"
    "            graph_driver = client.driver.clone(database=graph_name)\n"
    "            try:\n"
    "                episodic_node = await EpisodicNode.get_by_uuid(graph_driver, uuid)\n"
    "                found_driver = graph_driver\n"
    "                break\n"
    "            except Exception:  # noqa: BLE001 — absent in this graph, try the next\n"
    "                continue\n"
    "        if episodic_node is None or found_driver is None:\n"
    "            return ErrorResponse(error=f'Error deleting episode: node {uuid} not found')\n"
    "        await episodic_node.delete(found_driver)\n"
    "        return SuccessResponse(message=f'Episode with UUID {uuid} deleted successfully')\n"
)

OLD_GET_EPISODES = (
    "        if effective_group_ids:\n"
    "            episodes = await EpisodicNode.get_by_group_ids(\n"
    "                client.driver, effective_group_ids, limit=max_episodes\n"
    "            )\n"
)
NEW_GET_EPISODES = (
    "        if effective_group_ids:\n"
    "            # PATCH (ai-memory): on FalkorDB every group_id is its own graph; the\n"
    "            # shared driver stays bound to default_db (or the last-written group),\n"
    "            # so querying it with a group filter finds nothing. Clone the driver per\n"
    "            # group and query that group's graph. Upstream: getzep/graphiti#1651.\n"
    "            episodes = []\n"
    "            for effective_group_id in effective_group_ids:\n"
    "                group_driver = client.driver.clone(database=effective_group_id)\n"
    "                episodes.extend(\n"
    "                    await EpisodicNode.get_by_group_ids(\n"
    "                        group_driver, [effective_group_id], limit=max_episodes\n"
    "                    )\n"
    "                )\n"
    "            episodes = episodes[:max_episodes]\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if MARKER in src:
        print("graphiti_mcp_server.py: episode tools already patched")
        return 0
    fragments = (
        (OLD_DELETE_SIG, NEW_DELETE_SIG, "delete-episode-signature"),
        (OLD_DELETE_BODY, NEW_DELETE_BODY, "delete-episode-body"),
        (OLD_GET_EPISODES, NEW_GET_EPISODES, "get-episodes-group-loop"),
    )
    for old, _, label in fragments:
        n = src.count(old)
        if n != 1:
            print(f"graphiti_mcp_server.py: expected 1 match for {label}, found {n} — aborting",
                  file=sys.stderr)
            return 1
    for old, new, _ in fragments:
        src = src.replace(old, new)
    open(PATH, "w", encoding="utf-8").write(src)
    print("graphiti_mcp_server.py: group-graph-aware episode tools inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
