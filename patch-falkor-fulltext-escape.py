#!/usr/bin/env python3
"""
Build-time patch: harden the FalkorDB fulltext query builder (search_ops.py).

graphiti-core 0.28.2's FalkorDB search path builds RediSearch queries without real
escaping, producing "RediSearch: Syntax error at offset ..." failures that dead-letter
episodes. Four defects, fixed the way upstream does (or proposes to):

1. group_id escaping (backport of upstream PR #1549, released in 0.29.2): plain
   f'"{gid}"' quoting is not enough — RediSearch still trips over special characters
   (e.g. hyphens) inside the quotes. Escape every non-alphanumeric character with a
   backslash, exactly as upstream does.
2. Empty/stopword-only guard (upstream issues #1337/#1440, open): when sanitization +
   stopword filtering leaves no terms, the builder emits '(@group_id:"x") ()' — a
   syntax error. Return '' instead; every caller already treats '' as "skip fulltext"
   and returns []. Placed BEFORE the MAX_QUERY_LENGTH cap check.
3. Backtick separator (upstream issue #1440, open): '`' is a RediSearch-special
   character missing from _SEPARATOR_MAP; map it to a space like the other separators.
4. Punctuation-only token guard (found live 2026-07-14 during the dead-letter replay):
   a bare '_' term in the OR-list — e.g. '(backtest | diag_ | ... | h | _ | py)' from
   sanitized snake_case filenames — is a RediSearch syntax error ("Syntax error at
   offset 68 near h"). Underscore is added to the separator map (RediSearch tokenizes
   on '_' anyway, cf. PR #1549) and tokens without any alphanumeric character are
   dropped after stopword filtering.

While rewriting the cap-check fragment for (2), `len(group_ids or '')` is tidied to
`len(group_ids or [])` — identical semantics (counts group ids, 0 when None), but
list-typed instead of relying on len('').

The sibling patch patch-falkor-fulltext-escape-driver.py applies the same three fixes
to the duplicate builder in falkordb_driver.py.

Idempotent; fails the build loudly if the upstream code no longer matches.
"""
import sys

PATH = (
    "/app/mcp/.venv/lib/python3.11/site-packages/"
    "graphiti_core/driver/falkordb/operations/search_ops.py"
)

# Patch-specific idempotence marker (this file only carries this patch, but keep the
# marker specific anyway — never test on the generic 'PATCH (ai-memory)' prefix).
MARKER = "backport of upstream PR #1549"

OLD_IMPORT = (
    "import logging\n"
    "from typing import Any\n"
)
NEW_IMPORT = (
    "import logging\n"
    "# PATCH (ai-memory): re is needed for the group_id escaping below (backport of upstream PR #1549)\n"
    "import re\n"
    "from typing import Any\n"
)

OLD_ESCAPE = (
    "        escaped_group_ids = [f'\"{gid}\"' for gid in group_ids]\n"
)
NEW_ESCAPE = (
    "        # PATCH (ai-memory): escape every non-alphanumeric character in group_id\n"
    "        # (backport of upstream PR #1549) — bare quoting still breaks RediSearch on\n"
    "        # group ids containing hyphens and other special characters.\n"
    "        escaped_group_ids = ['\"' + re.sub(r'([^a-zA-Z0-9])', r'\\\\\\1', gid) + '\"' for gid in group_ids]\n"
)

OLD_GUARD = (
    "    filtered_words = [word for word in query_words if word and word.lower() not in STOPWORDS]\n"
    "    sanitized_query = ' | '.join(filtered_words)\n"
    "\n"
    "    if len(sanitized_query.split(' ')) + len(group_ids or '') >= max_query_length:\n"
    "        return ''\n"
)
NEW_GUARD = (
    "    filtered_words = [word for word in query_words if word and word.lower() not in STOPWORDS]\n"
    "    # PATCH (ai-memory): drop punctuation-only tokens — a bare '_' in the OR-list is a\n"
    "    # RediSearch syntax error (seen live 2026-07-14: \"Syntax error at offset 68 near h\"\n"
    "    # on '... | h | _ | py'); '_' is also mapped to a space in the separator map above\n"
    "    # for the same reason.\n"
    "    filtered_words = [w for w in filtered_words if any(ch.isalnum() for ch in w)]\n"
    "    sanitized_query = ' | '.join(filtered_words)\n"
    "\n"
    "    # PATCH (ai-memory): empty/stopword-only guard (upstream #1337/#1440) — without it\n"
    "    # the builder emits '(@group_id:\"x\") ()', an invalid RediSearch query.\n"
    "    if not sanitized_query:\n"
    "        return ''\n"
    "\n"
    "    if len(sanitized_query.split(' ')) + len(group_ids or []) >= max_query_length:\n"
    "        return ''\n"
)

OLD_SEPARATOR = (
    "        '/': ' ',\n"
    "        '\\\\': ' ',\n"
    "    }\n"
    ")\n"
)
NEW_SEPARATOR = (
    "        '/': ' ',\n"
    "        '\\\\': ' ',\n"
    "        # PATCH (ai-memory): backtick is a RediSearch-special character missing from the\n"
    "        # upstream separator map (upstream #1440) — treat it as a separator too. Same for\n"
    "        # underscore: RediSearch tokenizes on '_' anyway (cf. PR #1549), and mapping it\n"
    "        # to a space prevents bare-'_' tokens from reaching the query.\n"
    "        '`': ' ',\n"
    "        '_': ' ',\n"
    "    }\n"
    ")\n"
)


def main() -> int:
    src = open(PATH, encoding="utf-8").read()
    if MARKER in src:
        print("search_ops.py: fulltext escaping already patched")
        return 0
    fragments = (
        (OLD_IMPORT, NEW_IMPORT, "re-import"),
        (OLD_ESCAPE, NEW_ESCAPE, "group-id-escaping"),
        (OLD_GUARD, NEW_GUARD, "empty-query-guard"),
        (OLD_SEPARATOR, NEW_SEPARATOR, "backtick-separator"),
    )
    for old, _, label in fragments:
        n = src.count(old)
        if n != 1:
            print(f"search_ops.py: expected 1 match for {label}, found {n} — aborting",
                  file=sys.stderr)
            return 1
    for old, new, _ in fragments:
        src = src.replace(old, new)
    open(PATH, "w", encoding="utf-8").write(src)
    print("search_ops.py: group_id escaping + empty-query guard + backtick separator inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
