#!/usr/bin/env python3
"""
Build-time patch for graphiti-core 0.28.2's Anthropic LLM client.

Problem: the client fetches structured output with PLAIN forced tool-use
(`tool_choice: {type: tool}`) without `strict`. On some newer Claude models this
intermittently produces an invalid `tool_use.input` — an empty `{}` or the payload
wrapped in an unfilled `$PARAMETER_VALUE` placeholder — so graphiti's Pydantic
validation fails (`validation error for ExtractedEntities` / `NodeResolutions`) and
the episode is dropped after 2 retries. Measured failure ratio on entity-dense docs
was high enough to matter for a bulk import.

Fix: for structured-output calls, use the SDK helper `messages.parse()`. It turns the
Pydantic model into a strict json_schema (`output_config.format`); the decoder is then
schema-constrained and structurally cannot emit the malformed tool_use. Falls back to
the original tool_use path on any parse error (worst case == old behavior).

Idempotent. Requires an anthropic SDK new enough to expose parse()/output_config.
"""
import sys

PATH = "/app/mcp/.venv/lib/python3.11/site-packages/graphiti_core/llm_client/anthropic_client.py"

OLD = (
    "        try:\n"
    "            # Create the appropriate tool based on whether response_model is provided\n"
    "            tools, tool_choice = self._create_tool(response_model)\n"
)

NEW = (
    "        try:\n"
    "            # PATCH (ai-memory): structured output via SDK parse() -> strict json_schema\n"
    "            # (output_config.format), decoder-constrained so it cannot emit the malformed\n"
    "            # tool_use.input ({} or $PARAMETER_VALUE-wrapper) that plain forced tool_use\n"
    "            # produced on some newer Claude models, dropping episodes. Falls back to the\n"
    "            # original tool_use path on any parse error (worst case == prior behaviour).\n"
    "            if response_model is not None:\n"
    "                try:\n"
    "                    _pr = await self.client.messages.parse(\n"
    "                        system=system_message.content,\n"
    "                        max_tokens=max_creation_tokens,\n"
    "                        messages=user_messages_cast,\n"
    "                        model=self.model,\n"
    "                        output_format=response_model,\n"
    "                    )\n"
    "                    _u = getattr(_pr, 'usage', None)\n"
    "                    _in = (getattr(_u, 'input_tokens', 0) or 0) if _u else 0\n"
    "                    _out = (getattr(_u, 'output_tokens', 0) or 0) if _u else 0\n"
    "                    _parsed = getattr(_pr, 'parsed_output', None)\n"
    "                    if _parsed is not None:\n"
    "                        return _parsed.model_dump(), _in, _out\n"
    "                except anthropic.RateLimitError:\n"
    "                    raise\n"
    "                except Exception as _pe:\n"
    "                    logger.warning(f'parse() fallback to tool_use: {_pe}')\n"
    "\n"
    "            # Create the appropriate tool based on whether response_model is provided\n"
    "            tools, tool_choice = self._create_tool(response_model)\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if "PATCH (ai-memory)" in src:
        print("anthropic_client.py: already patched")
        return 0
    n = src.count(OLD)
    if n != 1:
        print(f"anthropic_client.py: expected 1 match, found {n} — patch aborted", file=sys.stderr)
        return 1
    open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("anthropic_client.py: parse() branch inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
