"""
Resolves a selected table to its sibling for a DIFFERENT reporting frequency
(monthly/quarterly/daily/fortnightly/annual) when the user's question names
one explicitly — e.g. "monthly sensitivity to securitization Part A" should
resolve to CIMS_RAQ_M_SEC9_SENSEC_PARTA even though every embedding/QA pair
was written against CIMS_RAQ_Q_SEC9_SENSEC_PARTA.

Why this is NOT a per-return regex
-----------------------------------
CIMS/RBI returns (see D:\\Repo5.5\\Database\\Returns.xml) number in the
hundreds, and only two families (RAQ, ALE) have been inspected so far. A rule
like "the frequency letter is always the 3rd underscore-token" would work for
those two and silently do nothing — or worse, do the wrong thing — for every
other return's table-naming convention.

So instead of a fixed rewrite, sibling relationships are DISCOVERED from the
live Oracle catalog (src.executor.cached_accessible_tables()): two tables are
frequency siblings if their names are identical except for exactly one
underscore-delimited token, and that token is drawn from the small, genuinely
universal FREQUENCY_TOKENS vocabulary (there are only ever ~5 reporting
cadences across these returns). This means a new return family gets frequency
support automatically the moment its tables exist in Oracle, with zero code
change here — and a return whose tables don't encode frequency this way just
yields no siblings, a safe no-op rather than a wrong guess.

Mirrors src/section_alias.py's shape: regex-detect an explicit reference in
the query text, resolve it against real table names, return a structured
result the caller treats as a hard override. Cached for the process lifetime,
same convention as section_alias._cache / retriever._index_cache.
"""

import logging
import re

from src import executor
from src.context import domain

log = logging.getLogger("frequency_alias")

# The domain vocabulary itself — not a per-return naming assumption. Both
# single-letter and spelled-out forms are included because different table
# families may encode frequency either way.
FREQUENCY_TOKENS = {
    "Q": "quarterly", "QUARTERLY": "quarterly",
    "M": "monthly", "MONTHLY": "monthly",
    "D": "daily", "DAILY": "daily",
    "F": "fortnightly", "FORTNIGHTLY": "fortnightly",
    "A": "annual", "ANNUAL": "annual", "ANNUALLY": "annual",
    "Y": "annual", "YEARLY": "annual",
}

_sibling_index_cache: dict | None = None


def _build_sibling_index(table_names) -> dict:
    """
    {TABLE_NAME_UPPER: {frequency_name: sibling_table_name}} built by
    clustering tables that differ from each other in exactly one token, where
    that token is a frequency token.

    Guards against false positives like PART_A / PART_B / PART_C (single-letter
    tokens that happen to overlap FREQUENCY_TOKENS, e.g. "A") by requiring
    EVERY differing token in the group to be a frequency token — B and C are
    not, so such groups are excluded outright — and by additionally skipping
    any position immediately preceded by the token "PART" as a second guard.
    """
    tokenized = {}
    for name in table_names:
        tokenized[name] = tuple(name.upper().split("_"))

    # masked_key -> {position_token: [table_name, ...]}
    groups: dict = {}
    for name, tokens in tokenized.items():
        for i, tok in enumerate(tokens):
            if i > 0 and tokens[i - 1] == "PART":
                continue
            masked = tokens[:i] + ("*",) + tokens[i + 1:]
            groups.setdefault(masked, {}).setdefault(tok, []).append(name)

    sibling_index: dict = {}
    for masked, token_map in groups.items():
        if len(token_map) < 2:
            continue
        if not all(tok in FREQUENCY_TOKENS for tok in token_map):
            continue
        # Build the frequency_name -> table mapping for this cluster. If two
        # different tokens map to the same frequency_name (e.g. "A" and
        # "ANNUAL" both present) prefer the first one seen — deterministic,
        # and this collision is not expected in practice.
        by_frequency: dict = {}
        for tok, names in token_map.items():
            freq = FREQUENCY_TOKENS[tok]
            for n in names:
                by_frequency.setdefault(freq, n)
        # Every table in this cluster shares the same sibling map.
        for names in token_map.values():
            for n in names:
                existing = sibling_index.setdefault(n, {})
                existing.update(by_frequency)

    return sibling_index


def _sibling_index() -> dict:
    global _sibling_index_cache
    if _sibling_index_cache is None:
        _sibling_index_cache = _build_sibling_index(executor.cached_accessible_tables())
    return _sibling_index_cache


def clear_cache():
    """Drop the cached sibling index — call after Oracle's table set changes
    and you want the next request to see it without a restart."""
    global _sibling_index_cache
    _sibling_index_cache = None


def detect_target_frequency(query: str) -> str | None:
    """The single unambiguous frequency named in `query`, or None if zero or
    more than one is mentioned — an ambiguous mention ("compare monthly and
    quarterly...") is out of scope for this override; the caller should leave
    the table untouched rather than guess."""
    hits = domain.detect_periodicity(query)
    if len(hits) == 1:
        return next(iter(hits))
    return None


def resolve_frequency_table(table_name: str, query: str) -> tuple[str, str | None]:
    """
    (final_table, warning_or_None).

    If the query names exactly one frequency and `table_name` has a real
    sibling at that frequency (per the live-catalog sibling index), returns
    the sibling. Otherwise returns `table_name` unchanged — with a warning
    only when a frequency WAS named but no sibling exists for it (the
    "requested frequency isn't available, showing what we have" case); no
    warning when no frequency was named at all, since that's the ordinary case.
    """
    target = detect_target_frequency(query)
    if target is None:
        return table_name, None

    siblings = _sibling_index().get(table_name.upper())
    if not siblings or target not in siblings:
        log.info("No %s sibling for %s — keeping original table", target, table_name)
        return table_name, (
            f"'{target}' data isn't available for this table — "
            f"showing the closest available frequency instead."
        )

    candidate = siblings[target]
    if candidate.upper() == table_name.upper():
        return table_name, None

    log.info("Resolved %s -> %s for requested frequency '%s'", table_name, candidate, target)
    return candidate, None


def apply_frequency_override(table_name: str, sql: str, query: str) -> tuple[str, str, str | None]:
    """
    (final_sql, final_table, warning_or_None).

    Call this AFTER `sql` has already been generated and validated against
    `table_name` — never before. `table_name` must be the CANONICAL table
    (the one schema.json/embeddings actually know the columns of); a
    frequency-sibling table is never in schema.json (it wasn't built from
    there — it was discovered from the live Oracle catalog), so
    src.sql_generator.validate_sql() cannot check its columns and would flag
    every real column as hallucinated if asked to validate against it
    directly. That is why the swap happens here, on already-validated SQL
    text, rather than by handing the sibling table to generate_sql/validate_sql.

    Column parity between siblings is a reasonable assumption (they are the
    same physical return at a different reporting cadence) but not a
    guarantee this module can derive from table names alone — so the
    substituted SQL is re-verified with a live Oracle EXPLAIN PLAN
    (src.executor.dry_run_sql) before being trusted. If that fails (or no
    sibling exists at all), the ORIGINAL (table_name, sql) is returned
    unchanged, with a warning.
    """
    candidate_table, warning = resolve_frequency_table(table_name, query)
    if candidate_table.upper() == table_name.upper():
        return sql, table_name, warning

    candidate_sql = re.sub(rf'\b{re.escape(table_name)}\b', candidate_table, sql, flags=re.IGNORECASE)
    ok, err = executor.dry_run_sql(candidate_sql)
    if ok:
        return candidate_sql, candidate_table, None

    log.info("Frequency swap %s -> %s failed live verification (%s) — keeping %s",
             table_name, candidate_table, err, table_name)
    target = detect_target_frequency(query)
    return sql, table_name, (
        f"'{target}' data isn't available for this table — "
        f"showing the closest available frequency instead."
    )
