#!/usr/bin/env python3
"""
dedup_episodes.py — detect and remove duplicate Episodic nodes, in place.

The in-place correction tool for the incremental-only regime: when an incident (e.g. a
partially-landed batch that got replayed) leaves duplicate episodes in a graph, this
removes them WITHOUT a rebuild. Duplicates are episodes in the same group that share
(name, source_description) — the same identity the idempotency key is derived from.

Removal goes through graphiti_core's own `remove_episode`, which cascades correctly:
  • an entity EDGE is deleted only if the removed episode is its creator (episodes[0]);
    facts shared with surviving episodes stay intact.
  • an entity NODE is deleted only when no other episode mentions it.
Keeping the OLDEST episode of a duplicate set (the default) therefore preserves the
shared facts: the oldest is the creator of record for edges the duplicates re-mention.

A duplicate set whose members differ in CONTENT is never touched (reported + skipped):
that is not a mechanical duplicate but two versions of something — a human call.

Runs INSIDE the graphiti-mcp container. Invoke via `memctl dedup …`, or directly:
  dedup_episodes.py --groups tooling                 # dry-run: report what would go
  dedup_episodes.py --groups tooling --apply         # actually remove (takes writer-lock)
  dedup_episodes.py --groups tooling --keep newest   # keep the newest instead

SAFETY: --apply acquires the same single-writer Redis lock (`aimem:writer-lock`) as
bulk_ingest, so it never mutates a graph concurrently with another writer; the durable
queue consumer pauses while it runs. Dry-run takes no lock (read-only).
"""
import sys, os, json, asyncio, argparse, socket
sys.path.insert(0, "/app/mcp/src")
from datetime import datetime, timezone
from collections import defaultdict
from urllib.parse import urlparse
from pydantic import BaseModel
import redis.asyncio as aioredis
from config.schema import GraphitiConfig
from services.factories import LLMClientFactory, EmbedderFactory, DatabaseDriverFactory
from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver

WRITER_LOCK = "aimem:writer-lock"
CONSUMER_HB = "aimem:heartbeat:consumer"
LOCK_TTL = 240  # same contract as bulk_ingest: refreshed at half-life, crash frees in <=4min


def redis_connect() -> aioredis.Redis:
    uri = os.environ.get("FALKORDB_URI", "redis://falkordb:6379")
    p = urlparse(uri)
    return aioredis.Redis(host=p.hostname or "falkordb", port=p.port or 6379,
                          password=os.environ.get("FALKORDB_PASSWORD") or None,
                          decode_responses=True)


async def acquire_lock(r: aioredis.Redis, owner: str) -> bool:
    return bool(await r.set(WRITER_LOCK, owner, nx=True, ex=LOCK_TTL))


async def release_lock(r: aioredis.Redis, owner: str) -> None:
    if await r.get(WRITER_LOCK) == owner:
        await r.delete(WRITER_LOCK)


async def _refresh_lock(r: aioredis.Redis, owner: str) -> None:
    while True:
        await asyncio.sleep(LOCK_TTL // 2)
        if await r.get(WRITER_LOCK) == owner:
            await r.expire(WRITER_LOCK, LOCK_TTL)


async def wait_for_consumer_idle(r: aioredis.Redis, timeout: int = 420) -> None:
    """Same contract as bulk_ingest: the consumer pauses on our lock; wait until it is
    not mid-write. Proceed (with a warning) if it is absent/stale or the wait caps."""
    waited = 0
    while waited < timeout:
        raw = await r.get(CONSUMER_HB)
        if not raw:
            return
        try:
            hb = json.loads(raw)
            ts = datetime.fromisoformat(hb["ts"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:  # noqa: BLE001
            return
        if hb.get("state") != "processing" or age > 90:
            return
        await asyncio.sleep(3)
        waited += 3
    print("  ! consumer still 'processing' after wait cap — proceeding "
          "(it should have paused on the writer lock)")


def build_client():
    """Build a Graphiti client exactly like the MCP server (same config/factories)."""
    config = GraphitiConfig()
    llm = LLMClientFactory.create(config.llm)
    emb = EmbedderFactory.create(config.embedder)
    dbc = DatabaseDriverFactory.create_config(config.database)
    driver = FalkorDriver(host=dbc["host"], port=dbc["port"],
                          password=dbc["password"], database=dbc["database"])
    custom = {et.name: type(et.name, (BaseModel,), {"__doc__": et.description})
              for et in config.graphiti.entity_types}
    g = Graphiti(graph_driver=driver, llm_client=llm, embedder=emb)
    return g, driver


async def scan_group(driver, grp: str):
    """Return {(name, source_description): [episode-dict, …]} for one group's graph."""
    drv = driver.clone(database=grp)
    res = await drv.execute_query(
        "MATCH (e:Episodic) RETURN e.uuid AS uuid, e.name AS name, "
        "e.source_description AS sd, e.content AS content, e.created_at AS created_at")
    records = res[0] if res else []
    sets = defaultdict(list)
    for rec in records:
        sets[(rec.get("name") or "", rec.get("sd") or "")].append(rec)
    return sets


def pick_removals(members: list[dict], keep: str) -> tuple[dict, list[dict]]:
    """Order a duplicate set deterministically and split into (kept, to_remove)."""
    ordered = sorted(members, key=lambda m: (str(m.get("created_at") or ""), m.get("uuid") or ""))
    kept = ordered[0] if keep == "oldest" else ordered[-1]
    return kept, [m for m in ordered if m is not kept]


async def main():
    ap = argparse.ArgumentParser(description="Remove duplicate episodes (same name + "
                                             "source_description) from group graphs, in place.")
    ap.add_argument("--groups", required=True, help="comma list of group_ids to dedup")
    ap.add_argument("--apply", action="store_true",
                    help="actually remove (default: dry-run report only)")
    ap.add_argument("--keep", choices=("oldest", "newest"), default="oldest",
                    help="which member of a duplicate set survives (default: oldest — "
                         "safest: it is the creator of record for shared edges)")
    a = ap.parse_args()
    groups = [x for x in a.groups.split(",") if x]

    r = redis_connect()
    g, driver = build_client()
    owner = f"dedup:{socket.gethostname()}:{os.getpid()}"
    refresher = None
    try:
        if a.apply:
            if not await acquire_lock(r, owner):
                held = await r.get(WRITER_LOCK)
                print(f"✗ another writer holds {WRITER_LOCK} ({held}) — refusing to start "
                      f"(single-writer). Wait for it to finish or clear the lock.",
                      file=sys.stderr)
                return 2
            refresher = asyncio.create_task(_refresh_lock(r, owner))
            print(f"✓ acquired writer lock as {owner}; waiting for the queue consumer to idle…")
            await wait_for_consumer_idle(r)

        skipped_content = 0
        removed_total = 0
        for grp in groups:
            sets = await scan_group(driver, grp)
            total = sum(len(v) for v in sets.values())
            dup_sets = {k: v for k, v in sets.items() if len(v) > 1}
            print(f"[{grp}] episodes={total} distinct={len(sets)} duplicate-sets={len(dup_sets)}")
            g.driver = driver.clone(database=grp)
            for (name, _sd), members in sorted(dup_sets.items()):
                if len({(m.get("content") or "") for m in members}) > 1:
                    skipped_content += 1
                    print(f"  ! SKIP '{name[:60]}' ×{len(members)}: same identity but "
                          f"DIFFERENT content — not a mechanical duplicate, resolve by hand")
                    continue
                kept, removals = pick_removals(members, a.keep)
                removed_total += len(removals)
                if not a.apply:
                    print(f"  · would remove {len(removals)}× '{name[:60]}' "
                          f"(keep {a.keep}: {kept['uuid']})")
                    continue
                for m in removals:
                    await g.remove_episode(m["uuid"])
                print(f"  ✓ removed {len(removals)}× '{name[:60]}' (kept {kept['uuid']})")
            if a.apply:
                after = await scan_group(driver, grp)
                n_total = sum(len(v) for v in after.values())
                n_dupes = sum(1 for v in after.values() if len(v) > 1)
                state = "✓ clean" if n_dupes == 0 else \
                        f"! {n_dupes} duplicate-set(s) remain (content-mismatch skips)"
                print(f"[{grp}] after: episodes={n_total} distinct={len(after)} — {state}")

        mode = "removed" if a.apply else "would remove"
        print(f"DEDUP {'DONE' if a.apply else 'DRY-RUN'} ({mode}={removed_total}"
              f"{f', content-mismatch skipped={skipped_content}' if skipped_content else ''})")
        return 3 if skipped_content else 0
    finally:
        if refresher:
            refresher.cancel()
        if a.apply:
            await release_lock(r, owner)
        # Graphiti's constructor spawns index-build tasks; let them finish instead of
        # tearing the loop down under them ("Task was destroyed but it is pending!").
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=15)
        await r.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
