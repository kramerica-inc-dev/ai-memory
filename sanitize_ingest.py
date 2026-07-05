#!/usr/bin/env python3
"""
Hygiene gate for the AI memory.

Sends episode text through a secret-sanitizer (POST /api/sanitize) BEFORE ingest, so that
secrets/PII never reach the extraction LLM or the knowledge graph.

Used by the migration and capture scripts. Stdlib-only (no dependencies).

Fail-closed: if the sanitizer is unreachable, text is NOT passed through unscrubbed — the
caller gets an exception (unless it explicitly opts into --allow-unsanitized).

This module expects a sanitizer service exposing a simple HTTP endpoint: POST /api/sanitize
with {"text": ..., "depth": ...} returning {"sanitized": ...}. A ready-made implementation you
can run behind SANITIZER_URL is:

    secret-sanitizer — https://github.com/kramerica-inc-dev/secret-sanitizer

Alternatives: bring your own service that speaks the same contract, or pass --no-sanitize to the
ingest scripts (NOT recommended for anything but a throwaway test).

CLI:
    echo "Jane Doe, IBAN NL91ABNA0417164300" | SANITIZER_URL=http://host:3100 \
        python3 sanitize_ingest.py --depth deep
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error


class SanitizerError(RuntimeError):
    """The sanitizer was unreachable or returned an error — ingest is blocked."""


def sanitize(text: str, depth: str = "standard", *, url: str | None = None,
             timeout: int = 30) -> str:
    """Return the scrubbed text. Raise SanitizerError on problems.

    depth: "quick" (credentials) | "standard" (+PII) | "deep" (+names/orgs).
    """
    base = (url or os.environ.get("SANITIZER_URL", "")).rstrip("/")
    if not base:
        raise SanitizerError(
            "SANITIZER_URL not set — cannot scrub text before ingest."
        )
    payload = json.dumps({"text": text, "depth": depth}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/sanitize",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    data = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < 4:
                time.sleep(2 ** attempt)          # 1,2,4,8s backoff on rate-limit/overload
                continue
            raise SanitizerError(f"Sanitizer error at {base}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SanitizerError(f"Sanitizer unreachable at {base}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SanitizerError(f"Invalid response from sanitizer: {exc}") from exc
    if data is None:
        raise SanitizerError(f"Sanitizer kept rate-limiting (429/503) at {base}.")

    if "sanitized" not in data:
        raise SanitizerError(f"Sanitizer response missing 'sanitized': {data!r}")
    return data["sanitized"]


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Scrub stdin through the secret-sanitizer.")
    p.add_argument("--depth", default="standard",
                   choices=["quick", "standard", "deep"])
    p.add_argument("--url", default=None, help="overrides SANITIZER_URL")
    p.add_argument("--allow-unsanitized", action="store_true",
                   help="pass unscrubbed text through if the sanitizer fails (NOT recommended)")
    args = p.parse_args(argv)

    text = sys.stdin.read()
    try:
        sys.stdout.write(sanitize(text, args.depth, url=args.url))
    except SanitizerError as exc:
        if args.allow_unsanitized:
            sys.stderr.write(f"[WARNING] {exc} — passed through unscrubbed.\n")
            sys.stdout.write(text)
            return 0
        sys.stderr.write(f"[BLOCKED] {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
