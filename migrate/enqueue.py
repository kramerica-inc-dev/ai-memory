#!/usr/bin/env python3
"""
enqueue.py — feed a JSONL dump into the durable queue for a SERIAL rebuild/backfill.

The second mass-ingest route, complementing bulk_ingest.py:
  • BULK (bulk_ingest.py) — one-pass onboarding via add_episode_bulk: fast, but a batch
    is all-or-nothing and dense content pushes it into timeout territory.
  • SERIAL (this tool)    — enqueue every record onto `aimem:queue`; the live consumer
    drains them ONE AT A TIME through the exact daily-use path (sanitizer, full
    per-episode dedup, bounded retry, dead-letter). Slower, but each episode stands
    alone: a failure quarantines that record only, never a batch. This is the proven
    route for dense documents and the sanctioned way to (re)build a graph.

This tool only ENQUEUES (fast, no LLM); the drain is the consumer's job. Progress is
visible via aimem:heartbeat:consumer and XLEN aimem:queue (migrate/reconcile.py shows
both). Idempotent: records whose key is already in the per-group idempotency set
(aimem:processed:<group>) are skipped, so a re-run tops up instead of duplicating.

Runs INSIDE the graphiti-mcp container. Invoke via `memctl enqueue …`, or directly:
  enqueue.py /tmp/chunks.jsonl                          # all groups
  enqueue.py /tmp/chunks.jsonl --groups tbo,trading     # only these groups
"""
import sys, os, json, asyncio, argparse, hashlib
from collections import Counter
from urllib.parse import urlparse
import redis.asyncio as aioredis

QUEUE = "aimem:queue"
PROCESSED_PREFIX = "aimem:processed:"   # per-group sets, shared with queue_service_durable


def _idem_key(group_id: str, name: str, source_description: str, content: str) -> str:
    """MUST match queue_service_durable._idempotency_key so the two paths share state."""
    raw = f"{group_id}|{name}|{source_description}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _processed_set(group_id: str) -> str:
    return f"{PROCESSED_PREFIX}{group_id}"


def redis_connect() -> aioredis.Redis:
    uri = os.environ.get("FALKORDB_URI", "redis://falkordb:6379")
    p = urlparse(uri)
    return aioredis.Redis(host=p.hostname or "falkordb", port=p.port or 6379,
                          password=os.environ.get("FALKORDB_PASSWORD") or None,
                          decode_responses=True)


async def main():
    ap = argparse.ArgumentParser(description="Enqueue a JSONL dump onto the durable queue.")
    ap.add_argument("jsonl", help="path to the JSONL dump file")
    ap.add_argument("--groups", default="", help="comma list; limit to these group_ids")
    a = ap.parse_args()

    want = {x for x in a.groups.split(",") if x} or None
    recs = []
    with open(a.jsonl, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if want and rec["group"] not in want:
                continue
            recs.append(rec)

    r = redis_connect()
    try:
        enq, skip = Counter(), Counter()
        for rec in recs:
            grp = rec["group"]
            key = _idem_key(grp, rec["name"], rec.get("source_description", ""), rec["content"])
            if await r.sismember(_processed_set(grp), key):
                skip[grp] += 1
                continue
            # Exact field shape of queue_service_durable.add_episode — the consumer
            # must not be able to tell a backfill from a daily MCP write.
            await r.xadd(QUEUE, {
                "group_id": grp, "name": rec["name"], "content": rec["content"],
                "source_description": rec.get("source_description", ""),
                "source": "text", "uuid": "", "key": key, "attempt": "0",
            })
            enq[grp] += 1
        depth = await r.xlen(QUEUE)
        for grp in sorted(set(enq) | set(skip)):
            print(f"[{grp}] enqueued={enq[grp]} skipped-already-processed={skip[grp]}")
        print(f"ENQUEUED {sum(enq.values())} (skipped {sum(skip.values())}); "
              f"queue depth now {depth}. The consumer drains serially — follow via "
              f"reconcile.py or XLEN {QUEUE}.")
        return 0
    finally:
        await r.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
