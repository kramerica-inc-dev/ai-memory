#!/usr/bin/env python3
"""
dead_letter.py — inspect and replay the ai-memory dead-letter stream (aimem:dead).

Quarantined episodes (extraction failures that survived retries on either write path)
land in aimem:dead with a deterministic `reason` category. They are NOT lost — but they
are inert until someone looks. This tool makes them visible and replayable, so "nothing
disappears silently" has an operational follow-through.

Runs INSIDE the graphiti-mcp container (needs redis + the FalkorDB it points at). Invoke
via `memctl dead-letter …`, or directly:
  dead_letter.py --list                        # entries + a reason breakdown
  dead_letter.py --replay                       # re-enqueue all (idempotent), drop from dead
  dead_letter.py --replay --reason truncation   # only that category
  dead_letter.py --replay --group tbo           # only that group
  dead_letter.py --purge --reason refusal --yes # drop WITHOUT replay (guarded)

Replay is idempotent: an entry whose idempotency key is already in its group's processed
set (it landed since it was parked) is dropped, not re-ingested. Re-enqueued items go back through
the normal consumer (single-writer, sanitizer, dead-letter) — so a replay can itself
re-quarantine a still-poison episode instead of looping forever.
"""
import sys, os, json, asyncio, argparse
from urllib.parse import urlparse
from collections import Counter
import redis.asyncio as aioredis

DEAD = "aimem:dead"
QUEUE = "aimem:queue"
PROCESSED_PREFIX = "aimem:processed:"   # per-group sets, shared with queue_service_durable


def _processed_set(group_id: str) -> str:
    return f"{PROCESSED_PREFIX}{group_id}"


def redis_connect() -> aioredis.Redis:
    uri = os.environ.get("FALKORDB_URI", "redis://falkordb:6379")
    p = urlparse(uri)
    return aioredis.Redis(host=p.hostname or "falkordb", port=p.port or 6379,
                          password=os.environ.get("FALKORDB_PASSWORD") or None,
                          decode_responses=True)


def _group(f: dict) -> str:
    return f.get("group_id") or f.get("group") or "?"


def _match(f: dict, reason: str, group: str) -> bool:
    if reason and f.get("reason", "other") != reason:
        return False
    if group and _group(f) != group:
        return False
    return True


async def main():
    ap = argparse.ArgumentParser(description="Inspect/replay the aimem:dead stream.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="show entries + reason breakdown")
    g.add_argument("--replay", action="store_true", help="re-enqueue (idempotent), drop from dead")
    g.add_argument("--purge", action="store_true", help="drop WITHOUT replay (needs --yes)")
    ap.add_argument("--reason", default="", help="filter to one reason category")
    ap.add_argument("--group", default="", help="filter to one group_id")
    ap.add_argument("--limit", type=int, default=0, help="cap entries processed (0 = all)")
    ap.add_argument("--yes", action="store_true", help="confirm --purge")
    a = ap.parse_args()

    r = redis_connect()
    try:
        entries = await r.xrange(DEAD)
        sel = [(eid, f) for eid, f in entries if _match(f, a.reason, a.group)]
        if a.limit:
            sel = sel[:a.limit]

        if a.list:
            total = len(entries)
            by_reason = Counter(f.get("reason", "other") for _, f in entries)
            by_group = Counter(_group(f) for _, f in entries)
            print(f"dead-letter {DEAD}: {total} entr{'y' if total == 1 else 'ies'}"
                  + (f"  (showing {len(sel)} after filter)" if (a.reason or a.group) else ""))
            print(f"  by reason: {dict(by_reason)}")
            print(f"  by group:  {dict(by_group)}")
            for eid, f in sel:
                print(f"  · {eid}  [{f.get('reason','other')}] {_group(f)} :: "
                      f"{f.get('name','?')[:60]}  — {f.get('error','')[:80]}")
            return 0

        if a.purge:
            if not a.yes:
                print(f"✗ --purge drops {len(sel)} entr{'y' if len(sel)==1 else 'ies'} WITHOUT "
                      f"replay. Re-run with --yes to confirm.", file=sys.stderr)
                return 1
            for eid, _ in sel:
                await r.xdel(DEAD, eid)
            print(f"purged {len(sel)} entr{'y' if len(sel)==1 else 'ies'} from {DEAD}")
            return 0

        # --replay
        replayed = already = skipped = 0
        for eid, f in sel:
            grp = _group(f)
            if grp == "?":
                # No usable group_id (malformed legacy entry, e.g. degraded by an earlier
                # replay round-trip). Re-enqueuing it would hand the consumer an empty
                # group_id — the exact feeder of the select_graph('') crash
                # (getzep/graphiti#1650). Keep it in the stream for manual triage.
                print(f"  ! skipped {eid}: no usable group_id "
                      f"(name={f.get('name', '?')[:50]!r}) — inspect, then --purge or fix by hand")
                skipped += 1
                continue
            key = f.get("key", "")
            if key and await r.sismember(_processed_set(grp), key):
                await r.xdel(DEAD, eid)          # landed since it was parked -> just drop
                already += 1
                continue
            await r.xadd(QUEUE, {
                "group_id": grp, "name": f.get("name", ""),
                "content": f.get("content", ""),
                "source_description": f.get("source_description", ""),
                "source": f.get("source", "text"), "uuid": f.get("uuid", ""),
                "key": key, "attempt": "0",
            })
            await r.xdel(DEAD, eid)
            replayed += 1
        print(f"replayed {replayed} to {QUEUE}; dropped {already} already-processed"
              + (f"; skipped {skipped} without usable group_id" if skipped else "") + ". "
              f"The consumer will drain them (single-writer, sanitizer, dead-letter still apply).")
        return 0
    finally:
        await r.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
