#!/usr/bin/env python3
"""
reconcile.py — level-triggered self-heal checks for the knowledge-graph memory.

The write path can drop or misplace work (in-memory queue, provider rate limits,
upstream bugs). Instead of trusting every write, this reconciler checks the graphs
against their invariants and repairs what is safe to repair — the k8s-controller
pattern: observe → diff → converge. Run it periodically (cron) or after incidents.

Checks per namespace (groups come from config/mapping.yaml):
  1. foreign episodes  — e.group_id != the graph they live in. CRITICAL: cross-group
     leakage should be impossible with the queue lock (patch-queue-lock.py); a nonzero
     count means the lock regressed or an unpatched writer is active.
  2. orphaned entities — Entity nodes without any incoming MENTIONS edge (debris from
     deleted episodes / failed extractions). Safe to remove: --fix sweeps them.
  3. pending embeddings — nodes flagged embedding_pending=true (deferred-embedding
     fallback wrote them without vectors). Reported so the backlog is visible.

Global:
  4. embedding-space drift — the `aimem:embedding_space` marker (written by
     memctl switch/reindex) vs the live embedder env. CRITICAL on mismatch: stored
     vectors are invalid for the live embedder; searches will silently degrade.

Artifact-level reconciliation (ledger vs landed chunks) lives in verify_index.py —
run that alongside this when artifact completeness matters.

Exit codes: 0 = clean · 1 = findings (fixable/informational) · 2 = critical.

Config (env, or memctl.conf next to the repo root via mapping_config.apply_conf):
  MEMORY_HOST  SSH target "user@host"; empty = local docker.
  MEMORY_DIR   Dir holding .env on the target (default: /opt/ai-memory).
  DOCKER       Docker binary on the target (default: docker).
  FALKOR_SVC   FalkorDB container name (default: ai-memory-falkordb-1).
  MCP_SVC      graphiti-mcp container name (default: ai-memory-graphiti-mcp-1).

Usage:
  python3 migrate/reconcile.py            # report only
  python3 migrate/reconcile.py --fix      # also sweep orphaned entities
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapping_config import load_mapping, apply_conf  # noqa: E402

apply_conf()

MEMORY_HOST = os.environ.get("MEMORY_HOST", "")
MEMORY_DIR = os.environ.get("MEMORY_DIR", "/opt/ai-memory")
DOCKER = os.environ.get("DOCKER", "docker")
FALKOR_SVC = os.environ.get("FALKOR_SVC", "ai-memory-falkordb-1")
MCP_SVC = os.environ.get("MCP_SVC", "ai-memory-graphiti-mcp-1")
SPACE_KEY = "aimem:embedding_space"


def _onhost(cmd: str, timeout: int = 40) -> str:
    if MEMORY_HOST:
        full = ["ssh", "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR", MEMORY_HOST, cmd]
    else:
        full = ["bash", "-c", cmd]
    try:
        return subprocess.run(full, capture_output=True, timeout=timeout, text=True).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _redis(args: str) -> str:
    return _onhost(
        f'set -a; . {MEMORY_DIR}/.env; set +a; '
        f'{DOCKER} exec {FALKOR_SVC} redis-cli -a "$FALKORDB_PASSWORD" --no-auth-warning {args} 2>/dev/null'
    )


def _graph_count(graph: str, query: str) -> int | None:
    """Run a RETURN count(...) query; None when the graph/query is unavailable."""
    out = _redis(f'GRAPH.QUERY {graph} "{query}"')
    for line in out.splitlines():
        if re.fullmatch(r"\d+", line.strip()):
            return int(line.strip())
    return None


def _live_embedder() -> str:
    env = _onhost(f"{DOCKER} exec {MCP_SVC} printenv 2>/dev/null")
    model = dims = ""
    for line in env.splitlines():
        if line.startswith("EMBEDDER_MODEL="):
            model = line.split("=", 1)[1]
        elif line.startswith("EMBEDDER_DIMENSIONS="):
            dims = line.split("=", 1)[1]
    return f"{model}/{dims}" if model and dims else ""


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Self-heal checks for the memory graphs.")
    ap.add_argument("--fix", action="store_true", help="sweep orphaned entities")
    a = ap.parse_args(argv)

    groups = load_mapping().groups
    findings = 0
    critical = 0

    print("▸ reconcile — invariants per namespace")
    for g in groups:
        foreign = _graph_count(
            g, f"MATCH (e:Episodic) WHERE e.group_id IS NOT NULL AND e.group_id <> \\\"{g}\\\" RETURN count(e)")
        orphans = _graph_count(g, "MATCH (n:Entity) WHERE NOT (n)<-[:MENTIONS]-() RETURN count(n)")
        pending = _graph_count(g, "MATCH (n) WHERE n.embedding_pending = true RETURN count(n)")
        line = f"  {g:<14} foreign={foreign if foreign is not None else '?'}  " \
               f"orphans={orphans if orphans is not None else '?'}  " \
               f"pending={pending if pending is not None else '?'}"
        if foreign:
            line += "   ✗ CROSS-GROUP LEAK — queue lock regressed or unpatched writer active"
            critical += 1
        if orphans:
            findings += 1
            if a.fix:
                _redis(f'GRAPH.QUERY {g} "MATCH (n:Entity) WHERE NOT (n)<-[:MENTIONS]-() DETACH DELETE n"')
                line += f"   → swept {orphans} orphans"
        if pending:
            findings += 1
        print(line)

    marker = _redis(f"GET {SPACE_KEY}").strip()
    live = _live_embedder()
    if not marker:
        print("  · embedding-space marker not set — run memctl switch/reindex once to record it")
        findings += 1
    elif marker != live:
        print(f"  ✗ EMBEDDING-SPACE DRIFT: graphs='{marker}' vs live='{live}' — stored vectors invalid")
        critical += 1
    else:
        print(f"  ✓ embedding space consistent ({marker})")

    if critical:
        print(f"→ CRITICAL: {critical} invariant(s) broken — stop bulk writes, investigate first")
        return 2
    if findings:
        print(f"→ {findings} finding(s){' (fixed where safe)' if a.fix else ' — rerun with --fix to sweep orphans'}")
        return 1
    print("✓ all invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
