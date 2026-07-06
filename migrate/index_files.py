#!/usr/bin/env python3
"""
index_files.py — the full-content index (Component B of the artifact layer).

For each relevant file under PROJECTS_DIR:  extract text -> scrub (sanitizer, 'quick')
-> chunk -> add_memory (Graphiti) with a group_id per area. Each episode is named
`relpath#chunkN` and carries source_description `artifact:<mirror-path>`, so a semantic
search result points back at the mirrored file (readable via your file-access layer).

Namespace routing (which area a top-level directory lands in) comes from the content-layer
config (config/mapping.yaml — see config/mapping.example.yaml), never from this script.

Idempotent through a ledger (migrate/.indexed.json) with  relpath -> {sha256, chunks, group}:
  - unchanged (sha match)  -> skip
  - new/changed            -> (re)index
  - vanished               -> marked 'stale' in the ledger (physically deleting old
                              episodes is a separate step, via FalkorDB — get_episodes
                              is unreliable)

Reuses backfill.py's MCP client + the sanitizer gate (fail-closed).

Usage:
  python3 migrate/index_files.py --area work --docs-only --dry-run   # explore
  python3 migrate/index_files.py --area work --docs-only             # index one area
  python3 migrate/index_files.py --path work/some-project            # one subdirectory
  SANITIZER_URL=http://your-sanitizer:8080 MCP_URL=http://your-memory-host:8000/mcp \
      python3 migrate/index_files.py --all                           # full rollout
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # backfill.py, mapping_config.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # sanitize_ingest.py
from backfill import MCPClient                                     # noqa: E402
from sanitize_ingest import sanitize, SanitizerError               # noqa: E402
from mapping_config import load_mapping, apply_conf                # noqa: E402

apply_conf()   # memctl.conf as env defaults (explicit env still wins) — parity with the shell tools

PROJECTS = Path(os.environ.get("PROJECTS_DIR", str(Path.home() / "Projects")))
# Where the mirror places files on the memory host — used only to build the artifact:
# back-reference in source_description (so a search result can be resolved to a real file).
ARTIFACTS = os.environ.get("ARTIFACTS_DIR", "/opt/ai-memory/artifacts")
MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
LEDGER = Path(__file__).resolve().parent / ".indexed.json"
LOCK = Path(__file__).resolve().parent / ".indexed.lock"

_MAPPING = None


def _mapping():
    """Load the content-layer mapping lazily (so --help works without a config file)."""
    global _MAPPING
    if _MAPPING is None:
        _MAPPING = load_mapping()
    return _MAPPING

# Directly-readable text formats. PDF/docx -> separate extraction (pdftotext/mammoth).
TEXT_EXT = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml",
            ".yml", ".toml", ".ini", ".cfg", ".sh", ".sql", ".html", ".css", ".csv"}
DOC_EXT = {".pdf", ".docx"}
DOCS_ONLY_EXT = {".md", ".txt", ".pdf", ".docx"}

# Directories/patterns we NEVER index (mirrors mirror.sh excludes + secrets).
EXCLUDE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
                "graphify-out", "dist", "build", ".next", ".claude", "chats"}
EXCLUDE_NAMES = {".env", ".env.local", ".DS_Store"}
EXCLUDE_SUFFIX = {".pyc", ".log", ".pem", ".key", ".p12", ".keystore", ".pfx", ".crt"}


# ── text extraction ────────────────────────────────────────────────────────

def extract_text(path: Path) -> str | None:
    """Raw text from a file, or None if it is not (currently) indexable."""
    suf = path.suffix.lower()
    if suf in TEXT_EXT:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
    if suf == ".pdf":
        return _run_extractor(["pdftotext", "-q", str(path), "-"])
    if suf == ".docx":
        # mammoth as a CLI module; falls back to None if not installed
        return _run_extractor([sys.executable, "-m", "mammoth", str(path)])
    return None


def _run_extractor(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=120)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.decode("utf-8", errors="ignore")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# ── chunking (greedy on paragraph/line boundaries, ~chunk_chars per chunk) ─

def chunk_text(text: str, chunk_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]
    chunks, buf, size = [], [], 0
    for para in text.split("\n\n"):
        para = para.rstrip()
        if not para:
            continue
        # paragraph itself too big -> split on line boundaries
        pieces = [para]
        if len(para) > chunk_chars:
            pieces, line_buf = [], []
            for line in para.split("\n"):
                if sum(len(x) for x in line_buf) + len(line) > chunk_chars and line_buf:
                    pieces.append("\n".join(line_buf)); line_buf = []
                line_buf.append(line)
            if line_buf:
                pieces.append("\n".join(line_buf))
        for piece in pieces:
            if size + len(piece) > chunk_chars and buf:
                chunks.append("\n\n".join(buf)); buf, size = [], 0
            buf.append(piece); size += len(piece) + 2
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


# ── file selection ──────────────────────────────────────────────────────────

def group_for(rel: Path) -> str:
    top = rel.parts[0] if rel.parts else ""
    return _mapping().group_for(top)


def iter_files(root: Path, exts: set[str]):
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        rel_parts = p.relative_to(PROJECTS).parts
        if any(part in EXCLUDE_DIRS for part in rel_parts[:-1]):
            continue
        if p.name in EXCLUDE_NAMES or p.suffix.lower() in EXCLUDE_SUFFIX:
            continue
        if p.name.startswith("~$"):                    # Word/Excel lock/temp files
            continue
        if p.name.startswith(".env") and p.name != ".env.example":
            continue
        if exts is not None and p.suffix.lower() not in exts:
            continue
        yield p


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ── ledger ─────────────────────────────────────────────────────────────────

def load_ledger() -> dict:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_ledger(led: dict) -> None:
    LEDGER.write_text(json.dumps(led, indent=2, ensure_ascii=False), encoding="utf-8")


# ── runner ─────────────────────────────────────────────────────────────────

def acquire_lock():
    """Advisory lock so two concurrent runs do not clobber each other's ledger.
    Return the fd, or None if another run holds the lock."""
    fd = LOCK.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        fd.close()
        return None


def run(root: Path, exts, dry_run, limit, chunk_chars, delay, do_sanitize):
    ledger = load_ledger()
    files = list(iter_files(root, exts))

    # Mark vanished files (physical episode cleanup is a separate step, via FalkorDB).
    # Only look within the current root — otherwise an --area run would wrongly mark ledger
    # entries of OTHER areas as 'stale'.
    present = {str(p.relative_to(PROJECTS)) for p in files}
    root_rel = "" if root == PROJECTS else str(root.relative_to(PROJECTS))
    def _under_root(rel: str) -> bool:
        return root_rel == "" or rel == root_rel or rel.startswith(root_rel + "/")
    stale = [rel for rel, meta in ledger.items()
             if isinstance(meta, dict) and not meta.get("stale")
             and _under_root(rel) and rel not in present]

    todo = []
    for p in files:
        rel = str(p.relative_to(PROJECTS))
        sha = sha256_of(p)
        prev = ledger.get(rel)
        if isinstance(prev, dict) and prev.get("sha256") == sha:
            continue                                       # unchanged
        todo.append((p, rel, sha, "reindex" if prev else "new"))

    if limit:
        todo = todo[:limit]

    by_group = {}
    for _, rel, _, _ in todo:
        g = group_for(Path(rel))
        by_group[g] = by_group.get(g, 0) + 1
    print(f"root={root}  to (re)index: {len(todo)} files  "
          f"(already current: {len(files) - len(todo)}, vanished: {len(stale)})")
    print("  per group_id:", by_group or "{}")

    if dry_run:
        for p, rel, _, kind in todo[:12]:
            txt = extract_text(p)
            n = len(chunk_text(txt, chunk_chars)) if txt else 0
            note = f"{n} chunk(s)" if txt is not None else "NO text (skip)"
            print(f"  · [{group_for(Path(rel))}] {rel}  ({kind}, {note})")
        if len(todo) > 12:
            print(f"    … +{len(todo) - 12} more")
        print("  (dry-run — nothing written)")
        return

    lock_fd = acquire_lock()
    if lock_fd is None:
        print("An index run is already active (lock held) — stopped to avoid ledger "
              f"corruption. Remove {LOCK} if no run is active.", file=sys.stderr)
        return

    client = MCPClient(MCP_URL)
    client.initialize()

    indexed = skipped = chunks_written = 0
    for i, (p, rel, sha, kind) in enumerate(todo, 1):
        text = extract_text(p)
        if text is None:
            skipped += 1
            continue
        chunks = chunk_text(text, chunk_chars)
        if not chunks:
            # empty file: mark as seen so we do not re-check it every run
            ledger[rel] = {"sha256": sha, "chunks": 0, "group": group_for(Path(rel))}
            continue
        group = group_for(Path(rel))
        artifact_path = f"{ARTIFACTS}/{rel}"
        wrote = 0
        for ci, chunk in enumerate(chunks):
            body = chunk
            if do_sanitize:
                try:
                    body = sanitize(chunk, depth="quick")      # strip credentials
                except SanitizerError as exc:
                    print(f"  [{i}] SKIP chunk {ci} (sanitizer): {rel} — {exc}")
                    continue                                   # fail-closed: do not index
            name = f"{rel}#chunk{ci}" if len(chunks) > 1 else rel
            try:
                client.add_memory(name, body, group, source_description=f"artifact:{artifact_path}")
                wrote += 1
                chunks_written += 1
            except Exception as exc:                           # noqa: BLE001
                print(f"  [{i}] ERROR add_memory: {name} — {exc}")
            time.sleep(delay)
        if wrote:
            ledger[rel] = {"sha256": sha, "chunks": wrote, "group": group}
            indexed += 1
            save_ledger(ledger)                                # persist per file (crash-safe)
        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} files, {chunks_written} chunks queued…")

    # mark vanished files in the ledger (the cleanup itself is a separate step)
    for rel in stale:
        ledger[rel]["stale"] = True
    save_ledger(ledger)
    print(f"Done: {indexed} files indexed ({chunks_written} chunks), "
          f"{skipped} skipped (no text). Ledger: {LEDGER}")
    if stale:
        print(f"  {len(stale)} vanished file(s) marked 'stale' "
              f"(physical episode cleanup is a separate step, via FalkorDB).")


def dump_chunks(root, exts, chunk_chars, do_sanitize, dump_path, delay=0.0):
    """Bulk-onboarding export: extract->sanitize->chunk, write JSONL instead of add_memory.
    One record per chunk: {group, name, content, source_description}. Consume with
    migrate/bulk_ingest.py (graphiti add_episode_bulk) — the fast onboarding route.
    Does NOT touch the MCP ledger/lock (this is an export, not index state).
    `delay` paces the sanitizer calls (limits HTTP 429 under load)."""
    files = list(iter_files(root, exts))
    n_files = n_chunks = skipped = 0
    with open(dump_path, "w", encoding="utf-8") as out:
        for i, p in enumerate(files, 1):
            rel = str(p.relative_to(PROJECTS))
            text = extract_text(p)
            if text is None:
                skipped += 1
                continue
            chunks = chunk_text(text, chunk_chars)
            if not chunks:
                continue
            group = group_for(Path(rel))
            artifact_path = f"{ARTIFACTS}/{rel}"
            for ci, chunk in enumerate(chunks):
                body = chunk
                if do_sanitize:
                    try:
                        body = sanitize(chunk, depth="quick")
                    except SanitizerError as exc:
                        print(f"  SKIP chunk {ci} (sanitizer): {rel} — {exc}")
                        continue
                name = f"{rel}#chunk{ci}" if len(chunks) > 1 else rel
                out.write(json.dumps({"group": group, "name": name, "content": body,
                                      "source_description": f"artifact:{artifact_path}"},
                                     ensure_ascii=False) + "\n")
                n_chunks += 1
                if delay:
                    time.sleep(delay)
            n_files += 1
            if i % 25 == 0:
                print(f"  {i}/{len(files)} files, {n_chunks} chunks dumped…")
    print(f"Dump done: {n_files} files ({n_chunks} chunks) -> {dump_path}; "
          f"{skipped} without text.")


def main(argv):
    p = argparse.ArgumentParser(description="Index file contents -> knowledge-graph memory (scrubbed).")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--all", action="store_true", help="the whole PROJECTS_DIR")
    sel.add_argument("--area", help="one top-level area (e.g. work, projects)")
    sel.add_argument("--path", help="subdirectory relative to PROJECTS_DIR (e.g. work/some-project)")
    p.add_argument("--docs-only", action="store_true",
                   help="only .md/.txt/.pdf/.docx (start small; less noise/cost)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="max number of files")
    p.add_argument("--chunk-chars", type=int, default=6000, help="~chunk size in chars (~1500 tokens)")
    p.add_argument("--delay", type=float, default=0.3, help="pause between add_memory calls (s)")
    p.add_argument("--no-sanitize", action="store_true", help="skip the sanitizer (NOT recommended)")
    p.add_argument("--dump", metavar="JSONL", help="export to JSONL instead of add_memory "
                   "(bulk-onboarding route; consume with migrate/bulk_ingest.py)")
    a = p.parse_args(argv)

    if a.all:
        root = PROJECTS
    elif a.area:
        root = PROJECTS / a.area
    else:
        root = PROJECTS / a.path
    if not root.exists():
        print(f"Does not exist: {root}", file=sys.stderr)
        return 2

    exts = DOCS_ONLY_EXT if a.docs_only else (TEXT_EXT | DOC_EXT)
    if a.dump:
        dump_chunks(root, exts, a.chunk_chars, not a.no_sanitize, a.dump, a.delay)
    else:
        run(root, exts, a.dry_run, a.limit, a.chunk_chars, a.delay, not a.no_sanitize)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
