#!/usr/bin/env python3
"""
Backfill: load existing (scrubbed) memory silos into the knowledge-graph memory.

Sources (both are examples of "onboard an existing store"; adapt to yours):
  - claude-mem session_summaries (SQLite)  -> episodes per area (group mapping)
  - a plain markdown directory              -> one episode per .md file

Every episode first goes through the secret-sanitizer (depth 'quick': strip credentials
but keep names/locations/projects) and is then written via the MCP add_memory tool into
Graphiti. Idempotent through a local ledger (migrate/.backfilled.txt): re-runs skip
already-done source items. add_memory is NOT given a uuid (that is for updates, not new).

Namespace routing (which area a source item lands in) comes from the content-layer config
(config/mapping.yaml — see config/mapping.example.yaml), never from this script.

Usage:
  python3 migrate/backfill.py --source markdown --dry-run
  python3 migrate/backfill.py --source claude-mem --limit 3
  SANITIZER_URL=http://your-sanitizer:8080 MCP_URL=http://your-memory-host:8000/mcp \
      python3 migrate/backfill.py --source all
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # mapping_config.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # sanitize_ingest.py
from sanitize_ingest import sanitize, SanitizerError  # noqa: E402
from mapping_config import load_mapping                # noqa: E402

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
CLAUDE_MEM_DB = os.environ.get("CLAUDE_MEM_DB", str(Path.home() / ".claude-mem" / "claude-mem.db"))
# Optional generic "markdown directory" source (e.g. an assistant's MEMORY.md / USER.md notes).
MARKDOWN_DIR = Path(os.environ.get("MARKDOWN_DIR", str(Path.home() / "notes")))
MARKDOWN_GROUP = os.environ.get("MARKDOWN_GROUP", "")   # empty -> default_group from mapping
LEDGER = Path(__file__).resolve().parent / ".backfilled.txt"


# ── MCP HTTP client (streamable HTTP + SSE) ─────────────────────────────────

class MCPClient:
    def __init__(self, base: str):
        self.base = base
        self.sid = None

    def _post(self, payload: dict) -> tuple[dict, dict]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["mcp-session-id"] = self.sid
        req = urllib.request.Request(self.base, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        parsed = {}
        for line in body.splitlines():
            if line.startswith("data:"):
                parsed = json.loads(line[5:].strip())
                break
        else:
            if body.strip():
                parsed = json.loads(body)
        return parsed, resp_headers

    def initialize(self):
        _, headers = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "backfill", "version": "1"}}})
        self.sid = headers.get("mcp-session-id")
        if not self.sid:
            raise RuntimeError("no mcp-session-id received")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def add_memory(self, name: str, body: str, group_id: str,
                   source_description: str = "backfill") -> dict:
        result, _ = self._post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "add_memory", "arguments": {
                "name": name, "episode_body": body, "group_id": group_id,
                "source": "text", "source_description": source_description}}})
        return result

    def get_episodes(self, group_id: str, max_episodes: int = 500) -> list[dict]:
        result, _ = self._post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_episodes", "arguments": {
                "group_ids": [group_id], "max_episodes": max_episodes}}})
        try:
            return json.loads(result["result"]["content"][0]["text"]).get("episodes", [])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            return []


# ── source readers (each episode carries a stable 'key' for the ledger) ─────

def markdown_episodes(mapping):
    """Generic adapter: one episode per .md file in MARKDOWN_DIR.

    Handy for onboarding an assistant's markdown notes (e.g. MEMORY.md / USER.md) or any
    flat folder of markdown. Lands in MARKDOWN_GROUP (or the mapping's default_group)."""
    group = MARKDOWN_GROUP or mapping.default_group
    if not MARKDOWN_DIR.exists():
        return
    for p in sorted(MARKDOWN_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8").strip()
        if text:
            yield {"name": f"{p.stem} ({MARKDOWN_DIR.name})", "body": text,
                   "group_id": group, "key": f"markdown:{p.name}", "src": "markdown"}


def claude_mem_episodes(mapping, limit: int | None):
    if not Path(CLAUDE_MEM_DB).exists():
        return
    con = sqlite3.connect(CLAUDE_MEM_DB)
    con.row_factory = sqlite3.Row
    q = ("SELECT id, COALESCE(merged_into_project, project) AS proj, request, investigated, "
         "learned, completed, next_steps, notes, created_at FROM session_summaries "
         "ORDER BY created_at_epoch DESC")
    if limit:
        q += f" LIMIT {int(limit)}"
    for r in con.execute(q):
        parts = []
        for lbl, col in [("Request", "request"), ("Investigated", "investigated"),
                         ("Learned", "learned"), ("Completed", "completed"),
                         ("Next", "next_steps"), ("Notes", "notes")]:
            v = (r[col] or "").strip()
            if v:
                parts.append(f"{lbl}: {v}")
        if not parts:
            continue
        proj = r["proj"] or mapping.default_group
        group = mapping.group_for(proj)
        body = f"Session ({r['created_at']}, project {proj}).\n" + "\n".join(parts)
        yield {"name": (r["request"] or f"Session {r['id']}")[:80], "body": body,
               "group_id": group, "key": f"claude-mem:summary:{r['id']}", "src": "claude-mem"}
    con.close()


# ── ledger ──────────────────────────────────────────────────────────────────

def _load_ledger() -> set[str]:
    return set(LEDGER.read_text(encoding="utf-8").split()) if LEDGER.exists() else set()


def _mark_done(key: str) -> None:
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(key + "\n")


# ── runner ──────────────────────────────────────────────────────────────────

def run(source, limit, dry_run, do_sanitize, delay):
    mapping = load_mapping()
    eps = []
    if source in ("markdown", "all"):
        eps += list(markdown_episodes(mapping))
    if source in ("claude-mem", "all"):
        eps += list(claude_mem_episodes(mapping, limit))

    done = _load_ledger()
    client = None
    if not dry_run:
        client = MCPClient(MCP_URL)
        client.initialize()
        # Reconcile: mark items already present in the graph (name match via get_episodes)
        # as done. This way only truly-landed episodes count; 503 failures stay open and are
        # retried on a later run — without duplicates.
        pending = [e for e in eps if e["key"] not in done]
        for g in {e["group_id"] for e in pending}:
            names = {ep.get("name", "") for ep in client.get_episodes(g)}
            for e in pending:
                if e["group_id"] == g and e["name"] in names and e["key"] not in done:
                    _mark_done(e["key"]); done.add(e["key"])

    todo = [e for e in eps if e["key"] not in done]
    by_group = {}
    for e in todo:
        by_group[e["group_id"]] = by_group.get(e["group_id"], 0) + 1
    print(f"{len(todo)} to backfill ({len(eps)-len(todo)} already landed). "
          f"source={source} sanitize={do_sanitize} dry_run={dry_run}")
    print("  per group_id:", by_group)
    if dry_run:
        for e in todo[:5]:
            print(f"  · [{e['group_id']}] {e['name']}  ({len(e['body'])} chars)")
        print("  (dry-run — nothing written)")
        return

    ok = skipped = 0
    for i, e in enumerate(todo, 1):
        body = e["body"]
        if do_sanitize:
            try:
                # 'quick' strips only credentials/tokens (those belong in a secret store);
                # keeps names/locations/projects. 'standard'/'deep' would remove those too.
                body = sanitize(body, depth="quick")
            except SanitizerError as exc:
                print(f"  [{i}] SKIP (sanitizer): {e['name'][:50]} — {exc}")
                skipped += 1
                continue
        try:
            client.add_memory(e["name"], body, e["group_id"])
            ok += 1
            if i % 10 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} queued…")
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"  [{i}] ERROR add_memory: {e['name'][:50]} — {exc}")
            skipped += 1
        time.sleep(delay)
    print(f"Done: {ok} queued, {skipped} skipped. Re-run later to reconcile landed items "
          f"and fill in the rest.")


def main(argv):
    p = argparse.ArgumentParser(description="Backfill silos -> knowledge-graph memory (scrubbed).")
    p.add_argument("--source", choices=["markdown", "claude-mem", "all"], default="all")
    p.add_argument("--limit", type=int, default=None, help="max number of claude-mem summaries")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-sanitize", action="store_true", help="skip the sanitizer (NOT recommended)")
    p.add_argument("--delay", type=float, default=0.3, help="pause between add_memory calls (s)")
    a = p.parse_args(argv)
    run(a.source, a.limit, a.dry_run, not a.no_sanitize, a.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
