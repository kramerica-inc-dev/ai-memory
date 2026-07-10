#!/usr/bin/env python3
"""
queue_purge.py — drop entries of given group(s) from aimem:queue or aimem:dead.

Companion to a SCOPED `memctl wipe --groups …`: the queue and the dead-letter are single
shared streams, but a namespace wipe must remove only ITS groups' entries — deleting the
whole stream would drop other groups' pending episodes (the same collateral class as the
old global idempotency set). This tool XDELs exactly the matching entries.

Note: an entry currently IN FLIGHT (delivered to the consumer, not yet acked) may still
complete and land in the just-wiped graph; for surgical precision run wipes while the
consumer is idle (reconcile shows the heartbeat state).

Runs INSIDE the graphiti-mcp container. Invoked by `memctl wipe --groups …`, or directly:
  queue_purge.py --stream queue --groups tbo,trading      # dry-run: report only
  queue_purge.py --stream dead  --groups tbo --yes        # actually delete
"""
import os, asyncio, argparse
from collections import Counter
from urllib.parse import urlparse
import redis.asyncio as aioredis

STREAMS = {"queue": "aimem:queue", "dead": "aimem:dead"}


def redis_connect() -> aioredis.Redis:
    uri = os.environ.get("FALKORDB_URI", "redis://falkordb:6379")
    p = urlparse(uri)
    return aioredis.Redis(host=p.hostname or "falkordb", port=p.port or 6379,
                          password=os.environ.get("FALKORDB_PASSWORD") or None,
                          decode_responses=True)


def _group(f: dict) -> str:
    return f.get("group_id") or f.get("group") or "?"


async def main():
    ap = argparse.ArgumentParser(description="Drop one or more groups' entries from a stream.")
    ap.add_argument("--stream", choices=sorted(STREAMS), required=True)
    ap.add_argument("--groups", required=True, help="comma list of group_ids to purge")
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry-run)")
    a = ap.parse_args()
    stream = STREAMS[a.stream]
    want = {x for x in a.groups.split(",") if x}

    r = redis_connect()
    try:
        entries = await r.xrange(stream)
        sel = [(eid, f) for eid, f in entries if _group(f) in want]
        by_group = Counter(_group(f) for _, f in sel)
        if not a.yes:
            print(f"{stream}: {len(entries)} entries, {len(sel)} match {sorted(want)} "
                  f"({dict(by_group)}) — dry-run, re-run with --yes to delete")
            return 0
        for eid, _ in sel:
            await r.xdel(stream, eid)
        print(f"{stream}: deleted {len(sel)} entr{'y' if len(sel) == 1 else 'ies'} "
              f"({dict(by_group)}); {await r.xlen(stream)} remain")
        return 0
    finally:
        await r.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
