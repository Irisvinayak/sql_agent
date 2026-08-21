"""
One-command pipeline for onboarding a NEW RBI return's tables into the
embeddings this app actually reads at query time
(EMBEDDING_DIR = embedding_building/cims_raq_quarterly, src/config.py).

Precondition -- must already be true before you run this:
  - The new return's tables/columns are described in data/.json-formatted,
    each column tagged with return_name exactly matching --return-name below.
  - The new return's XBRL taxonomy export JSON is saved somewhere under data/.
Both of those are manual data-entry steps this script does not do.

Usage:
    python embedding_building/cims_raq_quarterly/build_new_return.py \
        --return-name "CIMS_X(Quarterly)" \
        --taxonomy data/x_taxonomy.json

Add --skip-db if data/schema.sql and data/constraints.json are already fresh
and you don't want to hit Oracle again.

Steps run, in order (each step is the same script you'd run by hand -- this
just calls them in the right order and stops at the first failure):
  1. extract_schema.py            refresh data/schema.sql from live Oracle DDL
  2. extract_constraints.py       refresh data/constraints.json (whole schema,
                                   never filtered -- a filtered run would
                                   silently erase every other return's keys,
                                   since that script overwrites, not merges)
  3. build_schema.py              merge this return's tables into schema.json
                                   (existing returns are left untouched)
  4. build_embeddings.py          rebuild table/column/row-label indexes over
                                   the merged schema.json
  5. build_concept_map.py         rebuild concept_map.json from EVERY known
                                   taxonomy, not just this one -- build_concept_
                                   map.py only keeps what you pass it in a
                                   single run, so this script keeps a small
                                   registry (taxonomies.json, next to this
                                   file) of every return's taxonomy path ever
                                   built, and always passes the full list
  6. build_concept_embeddings.py  rebuild concept/member indexes + re-embed
                                   table_index.faiss with XBRL-enriched text
  7. build_bm25_index.py          rebuild the lexical (BM25) index

Deliberately NOT automated -- these need a human's judgment, not a script:
  - QA pairs (qa_pairs.json) + build_qa_index.py -- the single highest-
    weighted retrieval signal (QA_SIGNAL_WEIGHT=2.5); skipping this is why a
    freshly-onboarded return ranks worse than a mature one.
  - semantic_layer.yaml -- this return's real table joins.
  - business_dictionary.yaml -- this return's acronyms/section aliases.
See the reminder printed at the end.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TAXONOMY_REGISTRY = os.path.join(HERE, "taxonomies.json")


def run(step_no, total, label, script_args):
    print(f"\n{'=' * 70}\nSTEP {step_no}/{total}: {label}\n{'=' * 70}", flush=True)
    # -u (unbuffered) + PYTHONIOENCODING=utf-8: each step is a fresh subprocess
    # whose stdout is piped, not a terminal, so Python fully block-buffers by
    # default -- output (including real progress on long steps) would sit
    # invisible until a buffer flushed or the process exited, making a slow
    # step look identical to a hung one. utf-8 avoids a Windows console
    # (cp1252) crash on any non-ASCII character a step prints.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-u"] + script_args, cwd=ROOT, env=env)
    if result.returncode != 0:
        sys.exit(f"\n[ABORT] Step {step_no} ({label}) failed "
                  f"(exit code {result.returncode}). Fix the error above and "
                  f"re-run -- every step here is idempotent.")


def load_registry():
    if os.path.exists(TAXONOMY_REGISTRY):
        with open(TAXONOMY_REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_registry(registry):
    with open(TAXONOMY_REGISTRY, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--return-name", required=True,
                     help='Exact return_name as tagged in data/.json-formatted, '
                          'e.g. "CIMS_ALE_Domestic(Quarterly)"')
    ap.add_argument("--taxonomy", required=True,
                     help="Path to this return's XBRL taxonomy export JSON, "
                          "e.g. data/2057.json")
    ap.add_argument("--skip-db", action="store_true",
                     help="Skip extract_schema.py/extract_constraints.py and "
                          "use the data/schema.sql + data/constraints.json "
                          "already on disk.")
    ap.add_argument("--described-only", action="store_true",
                     help="Passed through to build_schema.py -- excludes live "
                          "tables that have no Excel description yet.")
    ap.add_argument("--with-member-index", action="store_true",
                     help="Also pass --with-member-index to build_concept_"
                          "embeddings.py (member index is unused today, "
                          "MEMBER_SIGNAL_WEIGHT=0 -- leave this off unless "
                          "you're specifically revisiting that signal).")
    ap.add_argument("--generate-qa", type=int, default=None, metavar="PER_TABLE",
                     help="After all indexes are rebuilt, also draft+validate+"
                          "merge QA pairs for this return and rebuild "
                          "qa_index.faiss, via add_qa_pairs.py --per-table "
                          "PER_TABLE. Omit to leave QA pairs untouched "
                          "(today's default, unchanged behavior).")
    ap.add_argument("--full-rebuild", action="store_true",
                     help="Force build_embeddings.py and build_concept_"
                          "embeddings.py to re-embed and re-sample EVERYTHING "
                          "(every table across every return, not just this "
                          "one), ignoring their caches. Normal onboarding "
                          "does not need this -- both scripts already skip "
                          "any table whose text/columns are unchanged since "
                          "the last build, which is what keeps onboarding one "
                          "new return fast. Use this only after changing "
                          "EMBED_MODEL, or a description-generation fix that "
                          "could plausibly change text for many tables at once.")
    args = ap.parse_args()

    taxonomy_path = args.taxonomy
    if not os.path.isabs(taxonomy_path):
        candidate = os.path.join(ROOT, taxonomy_path)
        if os.path.exists(candidate):
            taxonomy_path = candidate
    if not os.path.exists(taxonomy_path):
        sys.exit(f"[ABORT] Taxonomy file not found: {args.taxonomy}")

    base_total = 5 if args.skip_db else 7
    total = base_total + 1 if args.generate_qa is not None else base_total
    step = 1

    if not args.skip_db:
        run(step, total, "Refresh data/schema.sql from live Oracle DDL",
            [os.path.join(ROOT, "embedding_building", "extract_schema.py")])
        step += 1

        run(step, total,
            "Refresh data/constraints.json (whole schema -- deliberately no "
            "table filter, so no other return's PK/FK data is lost)",
            [os.path.join(ROOT, "embedding_building", "extract_constraints.py")])
        step += 1

    schema_args = [os.path.join(HERE, "build_schema.py"),
                   "--return-name", args.return_name]
    if args.described_only:
        schema_args.append("--described-only")
    run(step, total, f"Merge {args.return_name!r} tables into schema.json",
        schema_args)
    step += 1

    embeddings_args = [os.path.join(HERE, "build_embeddings.py")]
    if args.full_rebuild:
        embeddings_args.append("--full-rebuild")
    run(step, total,
        "Rebuild table_index / column_index / row_label_index from the "
        "merged schema.json" + (" (forced full rebuild)" if args.full_rebuild else " (incremental)"),
        embeddings_args)
    step += 1

    registry = load_registry()
    registry[args.return_name] = os.path.relpath(taxonomy_path, ROOT)
    save_registry(registry)

    concept_map_args = [os.path.join(HERE, "build_concept_map.py")]
    for rn, rel_path in registry.items():
        concept_map_args += ["--taxonomy", os.path.join(ROOT, rel_path)]
    run(step, total,
        f"Rebuild concept_map.json from all {len(registry)} known return(s) "
        f"({', '.join(registry.keys())}) -- taxonomies.json registry updated "
        f"so earlier returns are never dropped",
        concept_map_args)
    step += 1

    concept_emb_args = [os.path.join(HERE, "build_concept_embeddings.py")]
    if args.with_member_index:
        concept_emb_args.append("--with-member-index")
    if args.full_rebuild:
        concept_emb_args.append("--full-rebuild")
    run(step, total,
        "Rebuild concept_index + re-embed table_index with XBRL-enriched text"
        + (" (forced full rebuild)" if args.full_rebuild else " (incremental)"),
        concept_emb_args)
    step += 1

    run(step, total, "Rebuild the BM25 lexical index",
        [os.path.join(HERE, "build_bm25_index.py")])
    step += 1

    if args.generate_qa is not None:
        run(step, total,
            f"Draft, validate, and merge QA pairs for {args.return_name!r} "
            f"(add_qa_pairs.py --per-table {args.generate_qa})",
            [os.path.join(HERE, "add_qa_pairs.py"),
             "--return-name", args.return_name, "--per-table", str(args.generate_qa)])
        step += 1

    if args.generate_qa is not None:
        qa_reminder = (
            "  1. QA pairs   Done automatically above via add_qa_pairs.py "
            "(--generate-qa was passed).\n"
            f"                Review qa_pairs.json entries for {args.return_name!r} if "
            "you want extra scrutiny\n"
            "                beyond the automated schema/column/validate_sql/"
            "live-Oracle checks.\n"
        )
    else:
        qa_reminder = (
            "  1. QA pairs   Add 15-25 verified question/SQL examples for this return to\n"
            "                qa_pairs.json, then run:\n"
            "                    python embedding_building/cims_raq_quarterly/build_qa_index.py\n"
            "                This is the single highest-weighted retrieval signal\n"
            "                (QA_SIGNAL_WEIGHT=2.5) -- skipping it is the #1 reason a\n"
            "                freshly-onboarded return retrieves worse than an established one.\n"
        )

    print(f"""
{'=' * 70}
DONE -- {args.return_name!r} is now in schema.json and every dense/BM25 index.
{'=' * 70}

Still needed BY HAND before {args.return_name!r} is reliable, in order of impact:

{qa_reminder}
  2. Aliases    Add this return's acronyms/section-name aliases to
                embedding_building/business_dictionary.yaml, then run:
                    python scripts/validate_business_dictionary.py

  3. Joins      Review/add this return's real table relationships to
                embedding_building/cims_raq_quarterly/semantic_layer.yaml
                (undeclared joins are currently PERMITTED by default, not
                blocked, so leaving this empty is a silent risk, not a safe
                default).

  4. Restart    Restart the API process. src/retriever.py caches every index
                in memory for the life of the process -- none of today's
                rebuilt files are visible until it restarts.
""")


if __name__ == "__main__":
    main()
