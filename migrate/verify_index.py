#!/usr/bin/env python3
"""
verify_index.py — check that indexed chunks actually landed in the knowledge graph.

The artifact index (index_files.py) marks a file in the ledger as soon as its chunks are
*queued*. Processing happens server-side in Graphiti's in-memory queue; if graphiti restarts
mid-run, the not-yet-processed queue is lost -> the ledger says "done" but the chunks never
landed (silent loss). A single extraction error also drops a chunk.

This verify compares the ledger (path -> expected chunks) with the artifact episodes actually
present in FalkorDB (`source_description = artifact:<path>`, per group_id, via docker exec —
locally or over SSH). It reports 'gaps' (landed < expected). With --requeue the gap files are
cleaned up: partial episodes removed (DETACH DELETE) + ledger entry removed, so a later
index_files.py run re-queues them (without duplicates).

Config:
  MEMORY_HOST   SSH target "user@host" for a remote memory host; empty = local docker.
  MEMORY_DIR    Dir holding docker-compose.yml + .env on the target (default: /opt/ai-memory).
  DOCKER        Docker binary on the target (default: docker).
  FALKOR_SVC    FalkorDB container name (default: ai-memory-falkordb-1).
  ARTIFACTS_DIR Mirror path prefix used in source_description (default: /opt/ai-memory/artifacts).
  Namespaces come from config/mapping.yaml ('groups').

Usage:
  python3 migrate/verify_index.py                 # report (dry)
  python3 migrate/verify_index.py --requeue       # clean up gap files -> then index_files.py
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse extraction+chunking to recompute the EXPECTED chunk count (rather than trusting the
# ledger: it records the WRITTEN count, so a sanitizer skip under-counts there).
from index_files import extract_text, chunk_text, PROJECTS  # noqa: E402
from mapping_config import load_mapping                       # noqa: E402

LEDGER = Path(__file__).resolve().parent / ".indexed.json"
ARTIFACTS = os.environ.get("ARTIFACTS_DIR", "/opt/ai-memory/artifacts")
MEMORY_HOST = os.environ.get("MEMORY_HOST", "")               # empty -> local docker
MEMORY_DIR = os.environ.get("MEMORY_DIR", "/opt/ai-memory")
DOCKER = os.environ.get("DOCKER", "docker")
FALKOR_SVC = os.environ.get("FALKOR_SVC", "ai-memory-falkordb-1")


def _groups() -> list[str]:
    """Namespaces to verify, from the content-layer mapping (loaded lazily)."""
    return load_mapping().groups


def _onhost(remote_cmd: str, timeout: int = 40) -> str:
    """Run a command on the memory host (over SSH if MEMORY_HOST is set, else locally)."""
    if MEMORY_HOST:
        cmd = ["ssh", "-o", "ConnectTimeout=15", MEMORY_HOST, remote_cmd]
    else:
        cmd = ["bash", "-c", remote_cmd]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
        return out.stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _redis(graph: str, query: str) -> str:
    """redis-cli GRAPH.QUERY on the falkordb container; password sourced from the target's
    .env at call time (never printed, never stored locally)."""
    q = query.replace('"', '\\"')
    remote = (
        f'set -a; . {MEMORY_DIR}/.env; set +a; '
        f'{DOCKER} exec {FALKOR_SVC} redis-cli '
        f'-a "$FALKORDB_PASSWORD" --no-auth-warning GRAPH.QUERY {graph} "{q}" 2>/dev/null'
    )
    return _onhost(remote)


def landed_counts() -> Counter:
    """Number of landed artifact episodes per source_description path, across all groups."""
    counts: Counter = Counter()
    for g in _groups():
        out = _redis(g, 'MATCH (e:Episodic) WHERE e.source_description STARTS WITH "artifact:" '
                        'RETURN e.source_description')
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("artifact:"):
                counts[line[len("artifact:"):]] += 1
    return counts


def delete_episodes(group: str, artifact_path: str) -> None:
    p = artifact_path.replace('"', '\\"')
    _redis(group, f'MATCH (e:Episodic) WHERE e.source_description = "artifact:{p}" DETACH DELETE e')


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Verify that indexed chunks landed; requeue gaps.")
    ap.add_argument("--requeue", action="store_true",
                    help="clean up gap files (partial episodes + ledger entry removed)")
    a = ap.parse_args(argv)

    if not LEDGER.exists():
        print("No ledger — nothing to verify."); return 0
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    groups = _groups()
    landed = landed_counts()

    gaps = []          # (rel, expected, got, group)
    complete = 0
    for rel, meta in ledger.items():
        if not isinstance(meta, dict) or meta.get("stale"):
            continue
        # Expected count = re-chunk the file now (catches sanitizer skips the ledger under-counted);
        # falls back to the ledger value if the file is gone.
        src = PROJECTS / rel
        expected = int(meta.get("chunks", 0))
        if src.exists():
            txt = extract_text(src)
            if txt is not None:
                expected = max(expected, len(chunk_text(txt, 6000)))
        if expected == 0:
            continue
        got = landed.get(f"{ARTIFACTS}/{rel}", 0)
        if got >= expected:
            complete += 1
        else:
            gaps.append((rel, expected, got, meta.get("group", "?")))

    print(f"Ledger: {len(ledger)} files · complete: {complete} · gaps: {len(gaps)}")
    for rel, exp, got, g in gaps[:20]:
        print(f"  GAP [{g}] {rel}: {got}/{exp} chunks landed")
    if len(gaps) > 20:
        print(f"    … +{len(gaps) - 20} more")

    if not gaps:
        print("All complete — no requeue needed.")
        return 0
    if not a.requeue:
        print("(dry) run with --requeue to clean up gap files, then index_files.py again.")
        return 0

    for rel, exp, got, g in gaps:
        if got > 0 and g in groups:
            delete_episodes(g, f"{ARTIFACTS}/{rel}")     # partial episodes removed (anti-duplicate)
        ledger.pop(rel, None)                             # ledger entry removed -> re-queued
    LEDGER.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(gaps)} gap files cleaned up + removed from the ledger. Run index_files.py to requeue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
