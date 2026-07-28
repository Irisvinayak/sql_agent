"""
Builds a scoped schema.json for exactly one return: CIMS_RAQ(Quarterly).

Pipeline (all scoped to this one return, nothing else touched):
  1. Parse data/schema.sql (whole-DB DDL, already extracted) into {table: [{name,type}]}.
  2. Load column descriptions from data/.json-formatted (the Excel-sourced source of truth).
  3. Determine which tables belong to "CIMS_RAQ(Quarterly)" from the descriptions map itself
     (a table belongs to this return if ANY of its columns are tagged with this return_name
     in .json-formatted) — with a consistency check: if a table's columns disagree on
     return_name, it's flagged rather than silently resolved.
  4. Cross-check against tables that are actually live in Oracle right now (skip DDL-only
     ghost tables, and separately report tables that ARE live but have NO Excel description
     at all — these still get a schema entry, just without excel_name enrichment).
  5. Write the result to embedding_building/cims_raq_quarterly/schema.json

Usage:
    python embedding_building/cims_raq_quarterly/build_schema.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from embedding_building.parser import parse_sql_schema
from embedding_building.formatter import load_descriptions, build_schema_json

RETURN_NAME = "CIMS_RAQ(Quarterly)"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_SQL_PATH = os.path.join(ROOT, "data", "schema.sql")
DESCRIPTIONS_PATH = os.path.join(ROOT, "data", ".json-formatted")


def get_accessible_tables():
    """Live table names owned by the connected Oracle user (uppercase)."""
    from src.executor import get_accessible_tables as _get
    return _get()


def tables_for_return(descriptions: dict, return_name: str):
    """
    Determine table -> set(return_names seen) from the descriptions map,
    restricted to tables that have at least one column tagged with return_name.
    Returns (matched_table_names, inconsistent) where inconsistent is
    {table: {return_name, ...}} for any matched table whose columns disagree.
    """
    table_returns = {}
    for (table, _col), info in descriptions.items():
        rn = info.get("return_name", "").strip()
        if rn:
            table_returns.setdefault(table, set()).add(rn)

    matched = {t for t, rns in table_returns.items() if return_name in rns}
    inconsistent = {t: rns for t, rns in table_returns.items() if t in matched and len(rns) > 1}
    return matched, inconsistent


def main():
    print(f"Building scoped schema for return: {RETURN_NAME!r}\n")

    with open(SCHEMA_SQL_PATH, encoding="utf-8") as f:
        all_tables = parse_sql_schema(f.read())
    print(f"Parsed {len(all_tables)} tables total from {SCHEMA_SQL_PATH}")

    descriptions = load_descriptions(DESCRIPTIONS_PATH)
    described_table_names, inconsistent = tables_for_return(descriptions, RETURN_NAME)
    print(f"Tables tagged {RETURN_NAME!r} in .json-formatted: {len(described_table_names)}")

    if inconsistent:
        print(f"\n[WARN] {len(inconsistent)} table(s) have columns disagreeing on return_name "
              f"(possible Excel copy-paste mistagging) — kept in scope, but review these:")
        for t, rns in sorted(inconsistent.items()):
            print(f"    {t}: {sorted(rns)}")

    try:
        live_tables = get_accessible_tables()
        print(f"\nLive tables in Oracle: {len(live_tables)}")
    except Exception as e:
        print(f"\n[WARN] Could not reach Oracle to cross-check live tables ({e}); "
              f"proceeding without live-table filtering.")
        live_tables = None

    # Scope = described-as-Quarterly-RAQ tables that exist in the parsed DDL.
    # Also surface live CIMS_RAQ_Q_* tables that have NO Excel description at all,
    # so nothing silently goes missing from the scoped build.
    scoped_table_names = {t for t in described_table_names if t.lower() in all_tables}
    missing_from_ddl = described_table_names - scoped_table_names
    if missing_from_ddl:
        print(f"\n[WARN] {len(missing_from_ddl)} described table(s) not found in parsed DDL "
              f"(schema.sql may be stale): {sorted(missing_from_ddl)}")

    if live_tables is not None:
        live_undescribed = {
            t for t in live_tables
            if t.startswith("CIMS_RAQ_Q_") and t not in described_table_names
        }
        if live_undescribed:
            print(f"\n[INFO] {len(live_undescribed)} live CIMS_RAQ_Q_* table(s) have NO Excel "
                  f"description at all (excluded from this scoped build; add to .json-formatted "
                  f"if they belong to this return):")
            for t in sorted(live_undescribed):
                print(f"    {t}")

        before = len(scoped_table_names)
        scoped_table_names = {t for t in scoped_table_names if t in live_tables}
        dropped = before - len(scoped_table_names)
        if dropped:
            print(f"\n[INFO] Dropped {dropped} table(s) not currently live in Oracle (DDL-only/ghost).")

    scoped_tables = {t.lower(): all_tables[t.lower()] for t in scoped_table_names}
    print(f"\nFinal scoped table count: {len(scoped_tables)}")

    schema = build_schema_json(scoped_tables, descriptions)
    schema.sort(key=lambda t: t["table"])

    out_path = os.path.join(OUT_DIR, "schema.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
    print(f"\n-> {out_path} ({len(schema)} tables)")


if __name__ == "__main__":
    main()
