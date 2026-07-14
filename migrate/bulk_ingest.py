#!/usr/bin/env python3
"""
bulk_ingest.py — fast ONBOARDING / mass import via graphiti's add_episode_bulk.

Two ingest routes in this stack:
  • BULK (this tool)   — onboarding: read an existing content library in one pass.
    Dedups across the whole batch in a single pass -> orders of magnitude fewer
    LLM/embed/FalkorDB calls than per-episode, and avoids per-episode dedup growth.
    Slightly less thorough dedup than per-episode — acceptable for a one-off import.
  • MCP add_memory     — daily incremental use: full per-episode dedup.

Runs INSIDE the graphiti-mcp container (has graphiti_core + FalkorDB + embedder + config).
Reads a JSONL of records {group, name, content, source_description} — produced by
`index_files.py --dump` (artifacts) or `backfill.py --dump` (observations). Groups by
group_id and bulk-ingests per group in batches.

SAFETY (this is the mass-ingest path; it must match the daily path's guarantees):
  • SINGLE WRITER — acquires a cross-process Redis lock `aimem:writer-lock` (SET NX,
    auto-expiring). A second bulk run refuses to start, and the durable-queue consumer
    PAUSES while we hold it, so the two never write the same graph concurrently.
    The lock carries a TTL and is refreshed, so a crashed run frees it within the TTL.
  • IDEMPOTENT — before ingesting a record it checks the SAME idempotency key the queue
    uses (per-group set `aimem:processed:<group>`); already-ingested records are skipped,
    so a re-run or an overlapping run is a no-op instead of a source of duplicates.
  • NO SILENT LOSS — a batch that fails is written to the dead-letter stream
    `aimem:dead` (raw records + error), never dropped with a bare `continue`.
    There is deliberately NO in-batch retry: add_episode_bulk can PARTIALLY land
    episodes before raising, so re-submitting the same batch duplicates the landed
    ones (the processed-set is only marked after full-batch success). Recovery is the
    serial dead-letter replay (per-episode queue route), not an in-place retry.
  • OBSERVABLE — publishes `aimem:heartbeat:bulk` so a stall is visible (reconcile shows
    its age/progress) instead of looking identical to a slow-but-healthy run.

Usage (in the container):
  python bulk_ingest.py /tmp/chunks.jsonl                 # all groups
  python bulk_ingest.py /tmp/chunks.jsonl --groups work   # one group (validation)
  python bulk_ingest.py /tmp/chunks.jsonl --batch 25      # batch size per bulk call
"""
import sys, os, json, asyncio, argparse, hashlib, socket
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
from graphiti_core.utils.bulk_utils import RawEpisode
from graphiti_core.nodes import EpisodeType

WRITER_LOCK = "aimem:writer-lock"
PROCESSED_PREFIX = "aimem:processed:"   # per-group sets, shared with queue_service_durable
DEAD = "aimem:dead"
HEARTBEAT = "aimem:heartbeat:bulk"
CONSUMER_HB = "aimem:heartbeat:consumer"
LOCK_TTL = 240            # seconds; refreshed at half-life, so a crash/kill frees it in <=4min
                          # (the finally-block releases it immediately on a clean/handled exit)
# Deterministic failure categories for the dead-letter (mirrors queue_service_durable).
_FAILURE_MARKERS = (
    ("truncation", ("eof while parsing", "unterminated", "max_tokens", "truncat", "unexpected end")),
    ("sanitizer", ("sanitizer",)),
    ("rate_limit", ("rate limit", "429", "overloaded", "529", "quota", "temporarily")),
    ("timeout", ("timeout", "timed out")),
    ("connection", ("connection", "econnrefused", "connect")),
    ("refusal", ("refusalerror", "content policy", "blocked by")),
    ("validation", ("validation error", "field required", "pydantic", "value_error")),
)


def _classify_failure(msg: str) -> str:
    m = (msg or "").lower()
    for tag, needles in _FAILURE_MARKERS:
        if any(n in m for n in needles):
            return tag
    return "other"


def _idem_key(group_id: str, name: str, source_description: str, content: str) -> str:
    """MUST match queue_service_durable._idempotency_key so the two paths share state."""
    raw = f"{group_id}|{name}|{source_description}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _rec_key(r: dict) -> str:
    return _idem_key(r["group"], r["name"], r.get("source_description", ""), r["content"])


def _processed_set(group_id: str) -> str:
    return f"{PROCESSED_PREFIX}{group_id}"


def redis_connect() -> aioredis.Redis:
    uri = os.environ.get("FALKORDB_URI", "redis://falkordb:6379")
    p = urlparse(uri)
    return aioredis.Redis(host=p.hostname or "falkordb", port=p.port or 6379,
                          password=os.environ.get("FALKORDB_PASSWORD") or None,
                          decode_responses=True)


async def acquire_lock(r: aioredis.Redis, owner: str) -> bool:
    return bool(await r.set(WRITER_LOCK, owner, nx=True, ex=LOCK_TTL))


async def release_lock(r: aioredis.Redis, owner: str) -> None:
    # Release only if we still own it (best-effort CAS; TTL is the ultimate backstop).
    if await r.get(WRITER_LOCK) == owner:
        await r.delete(WRITER_LOCK)


async def _refresh_lock(r: aioredis.Redis, owner: str) -> None:
    while True:
        await asyncio.sleep(LOCK_TTL // 2)
        if await r.get(WRITER_LOCK) == owner:
            await r.expire(WRITER_LOCK, LOCK_TTL)


async def wait_for_consumer_idle(r: aioredis.Redis, timeout: int = 420) -> None:
    """Block until the durable-queue consumer is not mid-write, so we never overlap it.
    The consumer pauses when it sees our lock; we just wait for it to leave 'processing'.
    Proceeds anyway (with a warning) if the consumer is absent/stale or the wait caps."""
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


async def heartbeat(r: aioredis.Redis, **fields) -> None:
    try:
        await r.set(HEARTBEAT, json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), **fields}))
    except Exception:  # noqa: BLE001
        pass


async def check_embedder(g) -> None:
    """Reachability gate: a dead embedder (e.g. infinity down) would make every batch fail
    and mass dead-letter a whole drain. Do one tiny embed up front; abort loudly if it fails.
    Provider-agnostic (uses the same embedder the ingest will use)."""
    await g.embedder.create("ai-memory embedder health check")


async def graph_episode_count(r, grp: str) -> int | None:
    """Episodic-node count for a group, for the post-ingest completeness check."""
    try:
        reply = await r.execute_command(
            "GRAPH.RO_QUERY", grp, "MATCH (e:Episodic) RETURN count(e)")
    except Exception:  # noqa: BLE001
        return None
    stack = [reply]
    while stack:
        x = stack.pop()
        if isinstance(x, int):
            return x
        if isinstance(x, (list, tuple)):
            stack.extend(x)
        elif isinstance(x, (bytes, str)):
            s = x.decode() if isinstance(x, bytes) else x
            if s.lstrip("-").isdigit():
                return int(s)
    return None


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
    g = Graphiti(graph_driver=driver, llm_client=llm, embedder=emb,
                 max_coroutines=int(os.getenv("SEMAPHORE_LIMIT", "3")))
    return g, custom, config


async def ingest_batch(g, r, grp, batch, custom, now):
    """Ingest one batch idempotently; any failure dead-letters the whole batch.
    Returns (ingested_count, nodes, edges, dead_count)."""
    fresh = [rec for rec in batch if not await r.sismember(_processed_set(grp), _rec_key(rec))]
    if not fresh:
        return 0, 0, 0, 0
    eps = [RawEpisode(name=rec["name"], content=rec["content"],
                      source_description=rec.get("source_description", ""),
                      source=EpisodeType.text, reference_time=now) for rec in fresh]
    try:
        res = await g.add_episode_bulk(eps, group_id=grp, entity_types=custom)
        await r.sadd(_processed_set(grp), *[_rec_key(rec) for rec in fresh])
        n = len(getattr(res, "nodes", []) or [])
        e = len(getattr(res, "edges", []) or [])
        return len(fresh), n, e, 0
    except Exception as exc:  # noqa: BLE001
        # NO retry here: add_episode_bulk may have PARTIALLY landed episodes before the
        # exception, so re-submitting `eps` would duplicate them (observed: 60 dup
        # episodes in one rebuild). Quarantine the batch; the serial dead-letter replay
        # is the safe recovery path.
        msg = str(exc)
        reason = _classify_failure(msg)
        print(f"  [{grp}] batch FAIL: {type(exc).__name__}: {msg[:120]}")
        for rec in fresh:
            # Field shape mirrors queue_service_durable's queue entries (group_id, not
            # group; source/uuid/attempt present), so dead_letter.py --replay re-enqueues
            # a bulk casualty indistinguishably from a consumer casualty. A `group`-only
            # entry used to replay without a usable group_id — the consumer then defaulted
            # to '' and crashed on select_graph('') (getzep/graphiti#1650).
            await r.xadd(DEAD, {"group_id": grp, "name": rec["name"],
                                "content": rec["content"],
                                "source_description": rec.get("source_description", ""),
                                "source": "text", "uuid": "",
                                "key": _rec_key(rec), "attempt": "0",
                                "error": msg[:400], "reason": reason,
                                "failed_at": datetime.now(timezone.utc).isoformat()})
        print(f"  [{grp}] dead-lettered {len(fresh)} record(s) (reason={reason})")
        return 0, 0, 0, len(fresh)


async def main():
    ap = argparse.ArgumentParser(description="Bulk onboarding via add_episode_bulk.")
    ap.add_argument("jsonl", help="path to the JSONL dump file")
    ap.add_argument("--groups", default="", help="comma list; limit to these group_ids")
    ap.add_argument("--batch", type=int, default=25, help="chunks per add_episode_bulk call")
    a = ap.parse_args()

    want = {x for x in a.groups.split(",") if x} or None
    recs = defaultdict(list)
    with open(a.jsonl, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if want and rec["group"] not in want:
                continue
            recs[rec["group"]].append(rec)

    r = redis_connect()
    owner = f"bulk:{socket.gethostname()}:{os.getpid()}"
    if not await acquire_lock(r, owner):
        held = await r.get(WRITER_LOCK)
        print(f"✗ another writer holds {WRITER_LOCK} ({held}) — refusing to start "
              f"(single-writer). Wait for it to finish or clear the lock.", file=sys.stderr)
        return 2
    refresher = asyncio.create_task(_refresh_lock(r, owner))
    try:
        print(f"✓ acquired writer lock as {owner}; waiting for the queue consumer to idle…")
        await wait_for_consumer_idle(r)

        g, custom, config = build_client()
        print(f"provider={config.llm.provider}/{config.llm.model}  "
              f"embedder={config.embedder.provider}/{config.embedder.model}/{config.embedder.dimensions}")
        try:
            await check_embedder(g)
            print("✓ embedder reachable")
        except Exception as exc:  # noqa: BLE001
            print(f"✗ embedder unreachable ({type(exc).__name__}: {str(exc)[:120]}) — aborting "
                  f"before a drain that would mass dead-letter. Fix it and retry.", file=sys.stderr)
            return 2
        print(f"groups: { {k: len(v) for k, v in recs.items()} }")
        now = datetime.now(timezone.utc)
        grand_dead = 0
        gaps = 0

        for grp, items in recs.items():
            print(f"[{grp}] {len(items)} chunks -> bulk in batches of {a.batch}")
            done = tot_nodes = tot_edges = dead = skipped = 0
            for i in range(0, len(items), a.batch):
                batch = items[i:i + a.batch]
                ing, n, e, d = await ingest_batch(g, r, grp, batch, custom, now)
                done += ing; tot_nodes += n; tot_edges += e; dead += d
                skipped += len(batch) - ing - d
                await heartbeat(r, state="processing", group=grp,
                                done=done, total=len(items), dead=dead)
                print(f"  [{grp}] {done + skipped + dead}/{len(items)}  "
                      f"(+{n} nodes, +{e} edges; skipped={skipped}, dead={dead})")
            grand_dead += dead
            # Accounting invariant: every input record is ingested, skipped, or dead-lettered.
            if done + skipped + dead != len(items):
                gaps += 1
                print(f"  [{grp}] ✗ ACCOUNTING GAP: ingested+skipped+dead="
                      f"{done + skipped + dead} != input {len(items)}")
            # Completeness: on a fresh group the landed episode count must cover this run.
            landed = await graph_episode_count(r, grp)
            if landed is not None and landed < done:
                gaps += 1
                print(f"  [{grp}] ✗ SILENT DROP: only {landed} episodes in the graph "
                      f"but {done} ingested this run (graphiti dropped some)")
            print(f"[{grp}] done: ingested={done}, skipped={skipped}, dead={dead}, "
                  f"landed_in_graph={landed if landed is not None else '?'}, "
                  f"{tot_nodes} nodes, {tot_edges} edges")

        await heartbeat(r, state="done", dead=grand_dead)
        print(f"BULK DONE (dead-lettered={grand_dead}, gaps={gaps})")
        return 3 if (grand_dead or gaps) else 0
    finally:
        refresher.cancel()
        await release_lock(r, owner)
        await r.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
