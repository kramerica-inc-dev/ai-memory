#!/usr/bin/env python3
"""
Build-time patch: pass entity_types to the durable queue's initialize().

The durable queue (queue_service_durable.py, installed over services/queue_service.py)
processes entries recovered after a restart — before any add_episode call has run —
so it cannot capture entity_types lazily from the first call. The server has them at
init time; hand them over there.

Idempotent; fails the build loudly if upstream code no longer matches.
"""
import sys

PATH = "/app/mcp/src/graphiti_mcp_server.py"

OLD = (
    "    # Initialize queue service with the client\n"
    "    await queue_service.initialize(graphiti_client)\n"
)

NEW = (
    "    # Initialize queue service with the client\n"
    "    # PATCH (ai-memory): entity_types at init — the durable queue needs them for\n"
    "    # crash-recovered entries that predate any add_episode call in this process.\n"
    "    await queue_service.initialize(graphiti_client, entity_types=graphiti_service.entity_types)\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "entity_types=graphiti_service.entity_types" in src:
        print("graphiti_mcp_server.py: initialize al gepatcht")
        return 0
    n = src.count(OLD)
    if n != 1:
        print(f"graphiti_mcp_server.py: verwachtte 1 match, vond {n} — afgebroken", file=sys.stderr)
        return 1
    open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("graphiti_mcp_server.py: initialize(entity_types=…) ingevoegd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
