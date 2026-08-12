"""
Offline coverage check: does every genuinely categorical (low-cardinality
string) column in the live schema get picked up by
src/description_fetcher.py's LABEL_COLUMNS / _LABEL_KEYWORDS name-based
detection?

Why this exists: that detection is a hand-maintained keyword list with
nothing verifying it stays complete as tables are added to the return over
time. A categorical column that name-matching misses gets NO sample-value
injection into the generation prompt AND is invisible to
src/literal_validator.py's check_literal_validity() — the exact "hallucinated
literal" failure mode this codebase works hard to prevent, just for whatever
column happens to sit outside the keyword net. This is a build-time/CI safety
net (mirrors scripts/validate_business_dictionary.py's role for the alias
registry), NOT a runtime change — the request-time detection mechanism stays
untouched by this script.

Needs a live Oracle connection (same as description_fetcher.fetch_and_save).
Reports candidates for human review; never auto-registers anything, since a
human should confirm a column is genuinely a business category before it
starts appearing in prompts and being validated against.

Usage:
    python -m scripts.validate_label_coverage
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config
from src.description_fetcher import MAX_SAMPLES, _get_connection, _is_label_column


def _varchar_columns_needing_review(schema):
    """
    {table: [(column, type), ...]} for every VARCHAR/VARCHAR2 column NOT
    already matched by the existing name-based detection. These are the only
    candidates worth a live cardinality probe — a column already covered
    needs no further check, and non-string columns (NUMBER ids, RDATE) are
    never candidates for row-label sample injection in the first place.
    """
    out = {}
    for entry in schema:
        table = entry.get("table") or entry.get("table_name")
        for col in entry["columns"]:
            name = (col.get("name") or col.get("column_name") or "").lower()
            col_type = (col.get("type") or "").lower()
            if not name or not col_type.startswith("varchar"):
                continue
            if _is_label_column(name):
                continue
            out.setdefault(table, []).append((name, col_type))
    return out


def main():
    schema_path = f"{config.EMBEDDING_DIR}/schema.json"
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)

    candidates = _varchar_columns_needing_review(schema)
    total_candidate_cols = sum(len(v) for v in candidates.values())
    print(f"schema tables: {len(schema)}  "
          f"varchar columns outside the label keyword list: {total_candidate_cols}")

    if not candidates:
        print("Nothing to check — every varchar column is already covered by "
              "name-based detection.")
        return

    try:
        conn = _get_connection()
    except Exception as e:
        print(f"DB connection failed ({e}) — cannot run the cardinality probe. "
              f"This script needs live Oracle access, same as "
              f"description_fetcher.fetch_and_save().")
        sys.exit(1)

    findings = []
    try:
        cursor = conn.cursor()
        for table, cols in candidates.items():
            for col, col_type in cols:
                try:
                    cursor.execute(
                        f"SELECT COUNT(DISTINCT {col.upper()}) FROM {table.upper()} "
                        f"WHERE {col.upper()} IS NOT NULL"
                    )
                    (distinct_count,) = cursor.fetchone()
                except Exception as e:
                    print(f"  [skip] {table}.{col}: {e}")
                    continue
                # Low, non-zero cardinality is the categorical signature —
                # reuse MAX_SAMPLES (already description_fetcher.py's own
                # threshold for "few enough values to be a real category, not
                # free text") rather than introducing a second constant.
                if 0 < distinct_count <= MAX_SAMPLES:
                    findings.append((table, col, col_type, distinct_count))
    finally:
        cursor.close()
        conn.close()

    if not findings:
        print("\nNo coverage gaps found — every low-cardinality varchar column "
              "is already covered by name-based label detection.")
        return

    print(f"\n{len(findings)} POSSIBLE COVERAGE GAP(S) — review before adding "
          f"to LABEL_COLUMNS/_LABEL_KEYWORDS in src/description_fetcher.py:\n")
    for table, col, col_type, distinct_count in findings:
        print(f"  - {table}.{col} ({col_type}): {distinct_count} distinct value(s) "
              f"— looks categorical but no keyword matches this column name")


if __name__ == "__main__":
    main()
