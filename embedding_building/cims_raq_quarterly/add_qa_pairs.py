"""
One-command QA-pair automation: draft candidate question/SQL pairs for a
return, validate them, merge the survivors into qa_pairs.json, and rebuild
qa_index.faiss.

This wraps three EXISTING, unmodified tools in sequence -- it adds no new
generation or validation logic of its own:
  1. generate_return_questions.py   drafts candidate pairs via the `claude` CLI
  2. scripts/merge_qa_pairs.py      validates hard (table/columns exist in
                                     schema.json, validate_sql passes, and with
                                     --execute the SQL actually runs against
                                     live Oracle and returns real rows) and
                                     merges only the survivors
  3. build_qa_index.py              re-embeds qa_pairs.json into qa_index.faiss

No pair that fails any check in step 2 is ever written -- --write is only
ever passed alongside --execute, exactly as merge_qa_pairs.py already
requires.

Usage (standalone, any time, for an already-onboarded return):
    python embedding_building/cims_raq_quarterly/add_qa_pairs.py \
        --return-name "CIMS_ALE_Domestic(Quarterly)" --per-table 5

Also callable automatically from build_new_return.py via --generate-qa.

Restart the API process afterward -- src/retriever.py caches qa_index.faiss
in memory for the life of the process.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

sys.path.insert(0, ROOT)
from src import config  # noqa: E402


def run(step_no, total, label, script_args):
    print(f"\n{'=' * 70}\nSTEP {step_no}/{total}: {label}\n{'=' * 70}")
    result = subprocess.run([sys.executable] + script_args, cwd=ROOT)
    if result.returncode != 0:
        sys.exit(f"\n[ABORT] Step {step_no} ({label}) failed "
                  f"(exit code {result.returncode}). Fix the error above and "
                  f"re-run -- every step here is idempotent.")


def _embedding_dir() -> str:
    d = config.EMBEDDING_DIR
    return d if os.path.isabs(d) else os.path.join(ROOT, d)


def _count_pairs(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return len(json.load(f))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--return-name", required=True,
                     help='Exact (or partial, case-insensitive) return_name as '
                          'in schema.json, e.g. "CIMS_ALE_Domestic(Quarterly)"')
    ap.add_argument("--per-table", type=int, default=5,
                     help="Questions to draft per table (default: 5, same as "
                          "generate_return_questions.py's own default)")
    ap.add_argument("--model", default="sonnet",
                     help="Passed through to generate_return_questions.py "
                          "(default: sonnet)")
    ap.add_argument("--timeout", type=int, default=120,
                     help="Passed through to generate_return_questions.py, "
                          "per-table seconds (default: 120)")
    args = ap.parse_args()

    embedding_dir = _embedding_dir()
    qa_pairs_new = os.path.join(embedding_dir, "qa_pairs_new.json")
    qa_pairs_existing = os.path.join(embedding_dir, "qa_pairs.json")

    total = 3
    step = 1

    # qa_pairs_new.json is scratch/incoming, never source of truth -- clear it
    # first so a stale draft from a previous, unrelated run never gets
    # reprocessed alongside this return's fresh draft.
    if os.path.exists(qa_pairs_new):
        os.remove(qa_pairs_new)
        print(f"Cleared stale {qa_pairs_new}")

    run(step, total, f"Draft candidate QA pairs for {args.return_name!r} via `claude` CLI",
        [os.path.join(HERE, "generate_return_questions.py"),
         "--return-name", args.return_name,
         "--per-table", str(args.per_table),
         "--out", qa_pairs_new,
         "--model", args.model,
         "--timeout", str(args.timeout)])
    step += 1

    before_count = _count_pairs(qa_pairs_existing)

    run(step, total,
        "Validate candidate pairs (schema/column/validate_sql/live-Oracle-"
        "execution) and merge survivors into qa_pairs.json",
        [os.path.join(ROOT, "scripts", "merge_qa_pairs.py"), "--execute", "--write"])
    step += 1

    after_count = _count_pairs(qa_pairs_existing)
    merged_count = after_count - before_count

    run(step, total, "Rebuild qa_index.faiss from the merged qa_pairs.json",
        [os.path.join(HERE, "build_qa_index.py")])

    print(f"""
{'=' * 70}
DONE -- QA automation for {args.return_name!r}
{'=' * 70}

  Accepted  : {merged_count} new pair(s) merged into qa_pairs.json
              ({before_count} -> {after_count} total)
  Index     : qa_index.faiss rebuilt

Restart the API process now -- src/retriever.py caches qa_index.faiss in
memory for the life of the process; it will not see these new pairs until
restart.
""")


if __name__ == "__main__":
    main()
