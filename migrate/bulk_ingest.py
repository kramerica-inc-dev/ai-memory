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

Usage (in the container):
  python bulk_ingest.py /tmp/chunks.jsonl                 # all groups
  python bulk_ingest.py /tmp/chunks.jsonl --groups work   # one group (validation)
  python bulk_ingest.py /tmp/chunks.jsonl --batch 25      # batch size per bulk call
"""
import sys, os, json, asyncio, argparse
sys.path.insert(0, "/app/mcp/src")
from datetime import datetime, timezone
from collections import defaultdict
from pydantic import BaseModel
from config.schema import GraphitiConfig
from services.factories import LLMClientFactory, EmbedderFactory, DatabaseDriverFactory
from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.utils.bulk_utils import RawEpisode
from graphiti_core.nodes import EpisodeType


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
            r = json.loads(line)
            if want and r["group"] not in want:
                continue
            recs[r["group"]].append(r)

    g, custom, config = build_client()
    print(f"provider={config.llm.provider}/{config.llm.model}  "
          f"embedder={config.embedder.provider}/{config.embedder.model}/{config.embedder.dimensions}")
    print(f"groups: { {k: len(v) for k, v in recs.items()} }")
    now = datetime.now(timezone.utc)

    for grp, items in recs.items():
        print(f"[{grp}] {len(items)} chunks -> bulk in batches of {a.batch}")
        done = tot_nodes = tot_edges = 0
        for i in range(0, len(items), a.batch):
            batch = items[i:i + a.batch]
            eps = [RawEpisode(name=r["name"], content=r["content"],
                              source_description=r.get("source_description", ""),
                              source=EpisodeType.text, reference_time=now) for r in batch]
            try:
                res = await g.add_episode_bulk(eps, group_id=grp, entity_types=custom)
                n = len(getattr(res, "nodes", []) or []); e = len(getattr(res, "edges", []) or [])
                tot_nodes += n; tot_edges += e
            except Exception as exc:  # noqa: BLE001
                print(f"  [{grp}] ERROR batch @{i}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            done += len(batch)
            print(f"  [{grp}] {done}/{len(items)}  (+{n} nodes, +{e} edges)")
        print(f"[{grp}] done: {done} chunks, {tot_nodes} nodes, {tot_edges} edges")
    print("BULK DONE")


if __name__ == "__main__":
    asyncio.run(main())
