"""
Draft synthetic user question / gold-SQL pairs for one return's tables (e.g.
CIMS_ALE_Domestic(Quarterly)) by handing Claude each table's schema.json entry
plus its row-level label samples from description_samples.json, and asking it
to write realistic questions a bank/regulatory user would ask, with the Oracle
SQL that answers each one.

Calls the `claude` CLI directly (via subprocess, --print mode) so it reuses
whatever Claude access is already configured on this machine -- no separate
Anthropic API key needed.

Output matches the qa_pairs.json shape ({"table", "question", "sql"}) so it can
be reviewed by a human, edited, and merged into qa_pairs.json / re-embedded via
build_concept_embeddings.py's qa_index build. This is an OFFLINE authoring aid
only -- every generated pair should be spot-checked against the live Oracle
schema before merging, since Claude never sees actual row data, only the
distinct-value samples already captured in description_samples.json.

Usage:
    python -m embedding_building.cims_raq_quarterly.generate_return_questions \
        --return-name "CIMS_ALE_Domestic(Quarterly)" \
        --per-table 5 \
        --out embedding_building/cims_raq_quarterly/ale_qa_pairs.json
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = (
    "You are a senior business/regulatory analyst at an Indian bank who writes "
    "Oracle SQL against the bank's CIMS regulatory-return tables. You write "
    "realistic questions a compliance or MIS user would actually ask, and the "
    "exact SQL that answers them, using only the table/column names and "
    "row-label literal values you are given -- never invent a column, table, or "
    "label value that was not shown to you. Reply with ONLY the requested JSON, "
    "no prose, no markdown code fences."
)


def load_schema():
    with open(os.path.join(HERE, "schema.json"), encoding="utf-8") as f:
        return json.load(f)


def load_description_samples():
    path = os.path.join(HERE, "description_samples.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def tables_for_return(schema, return_name):
    needle = return_name.lower()
    return [t for t in schema if needle in t.get("return_name", "").lower()]


def build_table_context(table, samples):
    lines = [
        f"Table: {table['table']}",
        f"Description: {table.get('description', '')}",
        "Columns:",
    ]
    for col in table["columns"]:
        lines.append(f"  - {col['name']} ({col['type']}): {col.get('description', '')}")

    table_samples = samples.get(table["table"].lower(), {})
    if table_samples:
        lines.append("Sample row-label values (copy these EXACTLY, including spacing, when used as literals):")
        for col, values in table_samples.items():
            lines.append(f"  - {col}: {values}")
    return "\n".join(lines)


import shutil

CLAUDE_BIN = shutil.which("claude.cmd" if os.name == "nt" else "claude") or "claude"


def ask_claude(system_prompt, user_prompt, model, timeout):
    # user_prompt is multi-line, which breaks cmd.exe argv quoting on Windows --
    # pipe it over stdin instead (the CLI accepts a prompt via stdin in --print mode).
    result = subprocess.run(
        [
            CLAUDE_BIN, "-p",
            "--system-prompt", system_prompt,
            "--model", model,
            "--allowedTools", "",
        ],
        input=user_prompt,
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def generate_for_table(table, samples, per_table, model, timeout):
    context = build_table_context(table, samples)
    user_prompt = f"""{context}

Write {per_table} realistic, DISTINCT questions a bank compliance/MIS user would ask about
this table, each with the Oracle SQL that answers it.

Rules:
- Use only the exact table name and column names shown above.
- If you filter on a row-label column, use only the exact literal strings listed under
  "Sample row-label values" -- copy them verbatim (including trailing spaces).
- Vary phrasing and which columns/rows each question targets.
- Output ONLY a JSON array, no prose, no markdown fences, in exactly this shape:
[{{"table": "{table['table']}", "question": "...", "sql": "..."}}]
"""
    text = ask_claude(SYSTEM_PROMPT, user_prompt, model, timeout)
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  [WARN] could not parse output for {table['table']}, skipping")
        print(f"  ---- raw output ----\n{text[:500]}\n---------------------")
        return []


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--return-name", required=True,
                     help='Exact (or partial, case-insensitive) return_name to match in '
                          'schema.json, e.g. "CIMS_ALE_Domestic(Quarterly)"')
    ap.add_argument("--per-table", type=int, default=5,
                     help="Number of questions to generate per table (default: 5)")
    ap.add_argument("--out", default=None,
                     help="Output JSON path (default: <return-slug>_qa_pairs.json in this folder)")
    ap.add_argument("--model", default="sonnet",
                     help="Model alias/name passed to `claude --model` (default: sonnet)")
    ap.add_argument("--timeout", type=int, default=120,
                     help="Per-table timeout in seconds for the claude CLI call (default: 120)")
    args = ap.parse_args()

    schema = load_schema()
    samples = load_description_samples()
    tables = tables_for_return(schema, args.return_name)
    if not tables:
        sys.exit(f"[ABORT] No tables found with return_name matching {args.return_name!r} in schema.json")

    out_path = args.out or os.path.join(
        HERE, f"{args.return_name.split('(')[0].strip().lower().replace(' ', '_')}_qa_pairs.json"
    )

    print(f"[INFO] {len(tables)} table(s) matched {args.return_name!r}; "
          f"generating {args.per_table} question(s) each via `claude --model {args.model}`")

    all_pairs = []
    for i, table in enumerate(tables, 1):
        print(f"  [{i}/{len(tables)}] {table['table']} ...")
        try:
            pairs = generate_for_table(table, samples, args.per_table, args.model, args.timeout)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"  [WARN] {table['table']} failed: {exc}")
            pairs = []
        print(f"      -> {len(pairs)} pair(s)")
        all_pairs.extend(pairs)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_pairs, f, indent=2, ensure_ascii=False)

    print(f"[DONE] Wrote {len(all_pairs)} question/SQL pairs -> {out_path}")
    print("[NEXT] Review each pair against the live Oracle schema before merging "
          "into qa_pairs.json.")


if __name__ == "__main__":
    main()
