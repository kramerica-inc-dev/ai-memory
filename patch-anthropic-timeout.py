#!/usr/bin/env python3
"""
Build-time patch: bound each Anthropic request with a timeout.

graphiti-core 0.28.2 creates its AsyncAnthropic client with no `timeout`, so it uses the
SDK default (600s per request). On entity-dense chunks the extraction call can hang; with
`add_episode_bulk` a single hung call blocks the WHOLE batch, and the run stalls for tens of
minutes with idle CPU (observed: a 25-chunk batch not completing in 70+ min).

Fix: pass `timeout=LLM_REQUEST_TIMEOUT` (env, default 200s) to AsyncAnthropic. A hung/slow
call now fails fast -> graphiti's retry loop runs -> our durable-queue / bulk path retries
and, if it stays bad, DEAD-LETTERS it (visible, replayable) instead of hanging. This makes a
hung model call just another failure mode that self-corrects, rather than a stall.

Model-agnostic in intent: any provider gets the same "bound the request, quarantine on
persistent failure" posture. This patch covers the Anthropic client (the one in use); an
equivalent bound belongs on whatever provider client is active.

Idempotent; fails the build loudly if the upstream code no longer matches.
"""
import sys

PATH = "/app/mcp/.venv/lib/python3.11/site-packages/graphiti_core/llm_client/anthropic_client.py"

OLD = (
    "            self.client = AsyncAnthropic(\n"
    "                api_key=config.api_key,\n"
    "                max_retries=1,\n"
    "            )\n"
)

NEW = (
    "            self.client = AsyncAnthropic(\n"
    "                api_key=config.api_key,\n"
    "                max_retries=1,\n"
    "                # PATCH (ai-memory): bound each request (env LLM_REQUEST_TIMEOUT, default 200s;\n"
    "                # dense-document extractions have been observed to need >90s legitimately)\n"
    "                # so a hung/slow extraction call fails fast -> retry -> dead-letter, instead\n"
    "                # of a 600s SDK-default hang that stalls a whole add_episode_bulk batch.\n"
    "                timeout=float(os.environ.get('LLM_REQUEST_TIMEOUT', '200')),\n"
    "            )\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "LLM_REQUEST_TIMEOUT" in src:
        print("anthropic_client.py: timeout already patched")
        return 0
    n = src.count(OLD)
    if n != 1:
        print(f"anthropic_client.py: expected 1 AsyncAnthropic match, found {n} — aborting",
              file=sys.stderr)
        return 1
    open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("anthropic_client.py: request timeout inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
