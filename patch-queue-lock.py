#!/usr/bin/env python3
"""
Build-time patch: global episode lock in the MCP queue service.

Problem (discovered 2026-07-07 as production data corruption): graphiti-core
0.28.2's Graphiti.add_episode MUTATES the shared driver on the singleton client
(`self.driver = self.driver.clone(database=group_id)`, graphiti.py:889/1115).
The queue service runs one worker per group_id, but workers run concurrently
(asyncio). Interleaving at the await points lets worker A (group X) write through
the driver that worker B (group Y) just pointed at Y's graph -> episodes and their
extracted entities land in the WRONG graph while carrying the CORRECT group_id
property. Measured damage in one deployment: 153 episodes spread across all 7
namespaces — from normal concurrent MCP writes, not just bulk ingest.

Note: SEMAPHORE_LIMIT does NOT mitigate this — it throttles coroutines inside a
single add_episode, not the number of concurrent add_episode calls.

Fix: one global asyncio.Lock around `await process_func()` (async with -> released
on every path). Only one episode processes at a time across ALL groups. Throughput
becomes strictly sequential; correctness is guaranteed for as long as the upstream
driver mutation exists.

Idempotent; fails the build loudly if the expected code no longer matches (i.e. the
upstream image changed and this patch must be re-reviewed).
"""
import sys

PATH = "/app/mcp/src/services/queue_service.py"

OLD = (
    "                try:\n"
    "                    # Process the episode\n"
    "                    await process_func()\n"
)

NEW = (
    "                try:\n"
    "                    # PATCH (ai-memory): serialize episode processing across ALL groups.\n"
    "                    # graphiti-core 0.28.2 mutates the shared driver per group_id\n"
    "                    # (graphiti.py:889/1115) — concurrent group workers otherwise write\n"
    "                    # into each other's graph (cross-group corruption).\n"
    "                    if not hasattr(QueueService, '_global_episode_lock'):\n"
    "                        QueueService._global_episode_lock = asyncio.Lock()\n"
    "                    async with QueueService._global_episode_lock:\n"
    "                        # Process the episode\n"
    "                        await process_func()\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "PATCH (ai-memory)" in src:
        print("queue_service.py: already patched")
        return 0
    n = src.count(OLD)
    if n != 1:
        print(f"queue_service.py: expected 1 match, found {n} — aborting", file=sys.stderr)
        return 1
    open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("queue_service.py: global episode lock inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
