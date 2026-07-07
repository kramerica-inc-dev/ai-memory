#!/usr/bin/env python3
"""
Build-time patch: safe defaults on EdgeDuplicate (dedupe_edges.py).

Problem (2026-07-07): during a bulk re-ingest ~25% of episodes failed with
`1 validation error for EdgeDuplicate — contradicted_facts`. The Anthropic
messages.parse() path can silently return `parsed_output=None` (no exception,
so no fallback warning), after which the plain tool_use fallback yields a
response missing required fields. Because `duplicate_facts` and
`contradicted_facts` are required (`Field(...)`), one flaky dedup call drops
the ENTIRE episode.

Fix: `default_factory=list` on both fields. A missing field then means "no
duplicates/contradictions seen" — at worst slightly less sharp dedup for that
one call, instead of a lost episode. (Dedup misses are later caught by a
verify/reconcile cycle.)

Idempotent; fails the build loudly if upstream code no longer matches.
"""
import sys

PATH = "/app/mcp/.venv/lib/python3.11/site-packages/graphiti_core/prompts/dedupe_edges.py"

OLD = (
    "    duplicate_facts: list[int] = Field(\n"
    "        ...,\n"
    "        description='List of idx values of duplicate facts (only from EXISTING FACTS range). Empty list if none.',\n"
    "    )\n"
    "    contradicted_facts: list[int] = Field(\n"
    "        ...,\n"
)

NEW = (
    "    # PATCH (ai-memory): default_factory instead of required — one flaky structured-\n"
    "    # output call must not drop an entire episode (see patch-dedupe-defaults.py).\n"
    "    duplicate_facts: list[int] = Field(\n"
    "        default_factory=list,\n"
    "        description='List of idx values of duplicate facts (only from EXISTING FACTS range). Empty list if none.',\n"
    "    )\n"
    "    contradicted_facts: list[int] = Field(\n"
    "        default_factory=list,\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "PATCH (ai-memory)" in src:
        print("dedupe_edges.py: already patched")
        return 0
    n = src.count(OLD)
    if n != 1:
        print(f"dedupe_edges.py: expected 1 match, found {n} — aborting", file=sys.stderr)
        return 1
    open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("dedupe_edges.py: safe defaults set on EdgeDuplicate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
