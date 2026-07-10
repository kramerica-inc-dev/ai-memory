#!/usr/bin/env python3
"""
split_processed.py — one-time migration: legacy global idempotency set → per-group sets.

The durable queue used to track idempotency keys in ONE global set (`aimem:processed`).
That made `memctl wipe --groups X` collateral-delete every other group's keys (the set
can only be DELed whole — members are opaque hashes, not attributable to a group), after
which a replay/re-enqueue could re-ingest already-landed episodes as duplicates. Keys now
live per group (`aimem:processed:<group>`); this tool populates those sets from two
complementary sources:

  • --dump file.jsonl (repeatable) — recompute each record's key from its RAW content,
    byte-exact with what enqueue/bulk_ingest computed, and migrate it ONLY if the legacy
    global set contains it. A dump record absent from the legacy set has not landed yet
    (it may still be pending in the queue) and must NOT be marked processed.
  • --from-graph — recompute keys from the landed Episodic nodes per group. Graph content
    is post-sanitizer, so for records the sanitizer altered this yields the sanitized-
    variant key; it is added unconditionally (the episode demonstrably landed, and the
    variant key additionally guards a re-submit of the sanitized content).

Idempotent and additive (SADD): safe to run BEFORE a code cutover and AGAIN right after
it, so keys the old consumer added in between are caught. Dry-run by default; --apply
writes. --retire renames the legacy set to `aimem:processed-retired` afterwards (kept as
a backup; delete by hand once reconcile has been green for a while) and refuses if any
legacy member is not covered by a per-group set.

Runs INSIDE the graphiti-mcp container. Invoke via `memctl split-processed …`, or directly:
  split_processed.py --groups a,b --from-graph --dump /tmp/chunks.jsonl           # dry-run
  split_processed.py --groups a,b --from-graph --dump /tmp/chunks.jsonl --apply --retire
"""
import os, sys, json, asyncio, argparse, hashlib
sys.path.insert(0, "/app/mcp/src")
from urllib.parse import urlparse
import redis.asyncio as aioredis
from config.schema import GraphitiConfig
from services.factories import DatabaseDriverFactory
from graphiti_core.driver.falkordb_driver import FalkorDriver

LEGACY = "aimem:processed"
PREFIX = "aimem:processed:"
RETIRED = "aimem:processed-retired"   # outside the per-group prefix namespace


def _idem_key(group_id: str, name: str, source_description: str, content: str) -> str:
    """MUST match queue_service_durable._idempotency_key so all paths share state."""
    raw = f"{group_id}|{name}|{source_description}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def redis_connect() -> aioredis.Redis:
    uri = os.environ.get("FALKORDB_URI", "redis://falkordb:6379")
    p = urlparse(uri)
    return aioredis.Redis(host=p.hostname or "falkordb", port=p.port or 6379,
                          password=os.environ.get("FALKORDB_PASSWORD") or None,
                          decode_responses=True)


async def main():
    ap = argparse.ArgumentParser(description="Split the legacy global idempotency set "
                                             "into per-group sets.")
    ap.add_argument("--groups", required=True, help="comma list of group_ids")
    ap.add_argument("--dump", action="append", default=[],
                    help="JSONL dump whose records' keys to migrate (repeatable)")
    ap.add_argument("--from-graph", action="store_true",
                    help="also recompute keys from landed Episodic nodes")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: dry-run report only)")
    ap.add_argument("--retire", action="store_true",
                    help=f"after --apply, rename the legacy set to {RETIRED}")
    a = ap.parse_args()
    groups = [x for x in a.groups.split(",") if x]
    if not a.dump and not a.from_graph:
        print("✗ need at least one source: --dump file.jsonl and/or --from-graph",
              file=sys.stderr)
        return 1

    r = redis_connect()
    try:
        legacy = set(await r.smembers(LEGACY))
        add: dict[str, set] = {g: set() for g in groups}

        for path in a.dump:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    grp = rec["group"]
                    if grp not in add:
                        continue
                    key = _idem_key(grp, rec["name"],
                                    rec.get("source_description", ""), rec["content"])
                    if key in legacy:
                        add[grp].add(key)

        if a.from_graph:
            dbc = DatabaseDriverFactory.create_config(GraphitiConfig().database)
            driver = FalkorDriver(host=dbc["host"], port=dbc["port"],
                                  password=dbc["password"], database=dbc["database"])
            for grp in groups:
                drv = driver.clone(database=grp)
                res = await drv.execute_query(
                    "MATCH (e:Episodic) RETURN e.name AS name, "
                    "e.source_description AS sd, e.content AS content")
                for rec in (res[0] if res else []):
                    add[grp].add(_idem_key(grp, rec.get("name") or "",
                                           rec.get("sd") or "", rec.get("content") or ""))

        covered = set().union(*add.values())
        leftover = len(legacy - covered)
        for grp in groups:
            have = await r.scard(PREFIX + grp)
            print(f"[{grp}] keys to add: {len(add[grp])} (set currently holds {have})")
        print(f"legacy set: {len(legacy)} member(s); covered by this run: "
              f"{len(legacy & covered)}; unattributed leftover: {leftover}")
        if not a.apply:
            print("dry-run — re-run with --apply to write.")
            return 0

        for grp in groups:
            if add[grp]:
                await r.sadd(PREFIX + grp, *sorted(add[grp]))
        total = 0
        for grp in groups:
            total += await r.scard(PREFIX + grp)
        print(f"APPLIED: per-group sets now hold {total} key(s) across {len(groups)} group(s).")

        if a.retire:
            if leftover:
                print(f"! NOT retiring the legacy set: {leftover} member(s) are covered by "
                      f"no per-group set — investigate first (missing dump? group filter?).")
                return 1
            if legacy:
                await r.rename(LEGACY, RETIRED)
                print(f"retired legacy set -> {RETIRED} (backup; delete by hand later)")
            else:
                print("legacy set already absent — nothing to retire")
        return 0
    finally:
        await r.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
