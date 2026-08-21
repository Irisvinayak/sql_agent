"""
Builds vector embeddings (tables, columns, row-labels) for the merged
schema.json produced by build_schema.py — everything lands inside this
folder, completely independent of the legacy embedding_building/output/
indexes.

INCREMENTAL BY DEFAULT: table/column embeddings and row-label sampling both
skip any table whose embedding text (or, for row labels, whose set of label
columns) is unchanged since the last build — reusing the cached vectors /
samples instead of re-embedding or re-querying Oracle for them. This is what
keeps onboarding one new return fast even as the schema grows across many
returns: only genuinely new/changed tables actually cost anything. Pass
--full-rebuild to force re-embedding and re-sampling everything (needed after
changing EMBED_MODEL, or after a description-generation fix that could touch
many tables' text at once).

Run build_schema.py first. Then:
    python embedding_building/cims_raq_quarterly/build_embeddings.py
    python embedding_building/cims_raq_quarterly/build_embeddings.py --full-rebuild
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(OUT_DIR, "schema.json")

from embedding_building.formatter import build_vector_records
from src.vectorizer import embed_incremental, save_index
from src.description_fetcher import MAX_SAMPLES, _is_label_column


def fetch_row_label_samples(schema, old_samples=None, old_label_cols=None, old_needs_trim=None, force=False):
    """
    Same logic as src/description_fetcher.fetch_and_save, but scoped to this
    schema's tables and writing nowhere except the caller's return value (path is
    decided by the caller, not the shared EMBEDDING_DIR global).

    Returns (samples, needs_trim).

    Incremental: a table is skipped (its cached entry from old_samples reused
    verbatim) when its label-column set is identical to old_label_cols' record
    for that table — i.e. nothing about which columns need sampling has
    changed, so there is no reason to hit Oracle again for it. Pass force=True
    to re-sample every table regardless of cache (e.g. if you suspect the live
    values themselves have changed, not just the schema).

    Two bugs used to live in this copy, both because it reimplemented the shared
    logic instead of reusing it:

      * It matched only the exact LABEL_COLUMNS set, missing per-table variants
        that _is_label_column catches by substring — CATEGORY_INVESTMENT_DOM_OPR
        and MEMORANDUM_ITEM. Those two tables got no row labels at all, so the
        prompt never marked them VERTICAL and the model was free to SUM() across
        their label rows, double-counting the pre-aggregated total row.
      * It stripped sampled values, so a value stored as '     C2. Slipped to
        NPAs' was offered to the model without its padding and the resulting
        `= 'C2. Slipped to NPAs'` filter matched zero rows — silently, with no
        error. Values are now kept verbatim and padded columns are reported in
        needs_trim so the prompt can require TRIM().
    """
    import oracledb
    from src.config import DB_HOST, DB_PORT, DB_SERVICE, DB_USER, DB_PASSWORD

    old_samples = old_samples or {}
    old_label_cols = old_label_cols or {}
    old_needs_trim = old_needs_trim or {}

    samples = {}
    needs_trim = {}
    tables_to_query = []
    reused = 0
    for entry in schema:
        col_names = [c["name"].lower() for c in entry["columns"]]
        label_cols = sorted(c for c in col_names if _is_label_column(c))
        if not label_cols:
            continue
        if not force and entry["table"] in old_samples and old_label_cols.get(entry["table"]) == label_cols:
            samples[entry["table"]] = old_samples[entry["table"]]
            if entry["table"] in old_needs_trim:
                needs_trim[entry["table"]] = old_needs_trim[entry["table"]]
            reused += 1
        else:
            tables_to_query.append((entry, label_cols))

    print(f"  [cache] {reused}/{reused + len(tables_to_query)} tables' row-label samples "
          f"unchanged, reused as-is{' (forced full re-sample)' if force else ''}; "
          f"querying Oracle for {len(tables_to_query)}")

    if not tables_to_query:
        return samples, needs_trim

    dsn = oracledb.makedsn(DB_HOST, DB_PORT, service_name=DB_SERVICE)
    try:
        conn = oracledb.connect(user=DB_USER, password=DB_PASSWORD, dsn=dsn)
    except Exception as e:
        print(f"  [warn] DB connection failed: {e}")
        return samples, needs_trim

    try:
        cursor = conn.cursor()
        for entry, label_cols in tables_to_query:
            table = entry["table"].upper()
            table_samples = {}
            for col in label_cols:
                try:
                    cursor.execute(
                        f"SELECT DISTINCT {col.upper()} FROM {table} "
                        f"WHERE {col.upper()} IS NOT NULL AND ROWNUM <= :max_rows",
                        {"max_rows": MAX_SAMPLES},
                    )
                    raw_values = [str(r[0]) for r in cursor.fetchall() if r[0]]
                    values = [v for v in raw_values if v.strip()]
                    if values:
                        table_samples[col] = sorted(values)
                        if any(v != v.strip() for v in values):
                            needs_trim.setdefault(entry["table"], []).append(col)
                except Exception:
                    pass

            if table_samples:
                samples[entry["table"]] = table_samples
                padded = [c for c in table_samples if c in (needs_trim.get(entry["table"]) or [])]
                note = f"  (TRIM needed: {', '.join(padded)})" if padded else ""
                print(f"  [ok] {table}: sampled {sum(len(v) for v in table_samples.values())} values{note}")
            else:
                print(f"  [--] {table}: no live label samples")
    finally:
        cursor.close()
        conn.close()

    return samples, needs_trim


LABEL_COLS_CACHE_PATH = os.path.join(OUT_DIR, "label_columns_cache.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-rebuild", action="store_true",
                     help="Ignore every cache — re-embed all tables/columns and "
                          "re-sample every table's row labels from Oracle from "
                          "scratch. Use after changing EMBED_MODEL or a "
                          "description-generation fix that could touch many "
                          "tables' text at once. Normal runs should NOT need this.")
    args = ap.parse_args()

    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    print(f"Loaded {len(schema)} tables from {SCHEMA_PATH}\n")

    # ── Table + column embeddings (incremental) ─────────────────────────────
    table_records, column_records = build_vector_records(schema)
    print(f"Table records: {len(table_records)}")
    table_index, table_records = embed_incremental(
        table_records, key_fn=lambda r: r["table"], text_fn=lambda r: r["text"],
        index_path=os.path.join(OUT_DIR, "table_index.faiss"),
        meta_path=os.path.join(OUT_DIR, "table_meta.pkl"),
        force=args.full_rebuild,
    )
    save_index(table_index, table_records,
               os.path.join(OUT_DIR, "table_index.faiss"), os.path.join(OUT_DIR, "table_meta.pkl"))
    print(f"  -> table_index.faiss ({table_index.ntotal} vectors)\n")

    print(f"Column records: {len(column_records)}")
    column_index, column_records = embed_incremental(
        column_records, key_fn=lambda r: (r["table"], r["column"]), text_fn=lambda r: r["text"],
        index_path=os.path.join(OUT_DIR, "column_index.faiss"),
        meta_path=os.path.join(OUT_DIR, "column_meta.pkl"),
        force=args.full_rebuild,
    )
    save_index(column_index, column_records,
               os.path.join(OUT_DIR, "column_index.faiss"), os.path.join(OUT_DIR, "column_meta.pkl"))
    print(f"  -> column_index.faiss ({column_index.ntotal} vectors)\n")

    # ── Row-label sampling (incremental) + embedding ────────────────────────
    old_samples, old_needs_trim, old_label_cols = {}, {}, {}
    if not args.full_rebuild:
        samples_path = os.path.join(OUT_DIR, "description_samples.json")
        trim_path_existing = os.path.join(OUT_DIR, "needs_trim.json")
        if os.path.exists(samples_path):
            with open(samples_path, encoding="utf-8") as f:
                old_samples = json.load(f)
        if os.path.exists(trim_path_existing):
            with open(trim_path_existing, encoding="utf-8") as f:
                old_needs_trim = json.load(f)
        if os.path.exists(LABEL_COLS_CACHE_PATH):
            with open(LABEL_COLS_CACHE_PATH, encoding="utf-8") as f:
                old_label_cols = json.load(f)

    print(f"Fetching row-label samples from Oracle (scoped to these {len(schema)} tables)...")
    samples, needs_trim = fetch_row_label_samples(
        schema, old_samples=old_samples, old_label_cols=old_label_cols,
        old_needs_trim=old_needs_trim, force=args.full_rebuild,
    )
    with open(os.path.join(OUT_DIR, "description_samples.json"), "w", encoding="utf-8") as f:
        json.dump(samples, f, indent=2)
    print(f"  -> description_samples.json ({len(samples)} tables with samples)")

    # Persist this run's label-column sets so the NEXT run can tell whether
    # anything changed without needing the previous schema.json around.
    current_label_cols = {
        entry["table"]: sorted(c["name"].lower() for c in entry["columns"] if _is_label_column(c["name"].lower()))
        for entry in schema
        if any(_is_label_column(c["name"].lower()) for c in entry["columns"])
    }
    with open(LABEL_COLS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(current_label_cols, f, indent=2)

    # Always write needs_trim.json, even when empty: a stale file from an earlier
    # build would otherwise keep asking for TRIM() on columns that no longer need
    # it, and the prompt would carry an instruction that is simply wrong.
    trim_path = os.path.join(OUT_DIR, "needs_trim.json")
    with open(trim_path, "w", encoding="utf-8") as f:
        json.dump(needs_trim, f, indent=2)
    trim_count = sum(len(v) for v in needs_trim.values())
    print(f"  -> needs_trim.json ({trim_count} column(s) with whitespace-padded values)\n")

    if samples:
        print("Building row-label FAISS index...")
        label_records = []
        for table, col_map in samples.items():
            for col, values in col_map.items():
                for val in values:
                    label_records.append({
                        "table": table, "column": col, "value": val,
                        "text": f"{table} {col} label: {val}",
                    })
        if label_records:
            label_index, label_records = embed_incremental(
                label_records,
                key_fn=lambda r: (r["table"], r["column"], r["value"]),
                text_fn=lambda r: r["text"],
                index_path=os.path.join(OUT_DIR, "row_label_index.faiss"),
                meta_path=os.path.join(OUT_DIR, "row_label_meta.pkl"),
                force=args.full_rebuild,
            )
            save_index(label_index, label_records,
                       os.path.join(OUT_DIR, "row_label_index.faiss"), os.path.join(OUT_DIR, "row_label_meta.pkl"))
            print(f"  -> row_label_index.faiss ({label_index.ntotal} vectors)")
    else:
        print("No row-label samples found — skipping row-label index.")

    print("\nDone.")


if __name__ == "__main__":
    main()
