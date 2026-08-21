# Command Reference

Every terminal command used to run, build, test, and validate this application, with
its purpose and a runnable example. Commands are run from the repo root (`d:\sql query gen`)
unless noted otherwise.

---

## Running the application

### Start the API server
**Purpose:** Serves `/api/query` and `/api/health` for the frontend.
```
python -m api.main
```
Reads `EMBEDDING_DIR` at startup and logs whether `qa_index.faiss` exists. Runs on
`http://localhost:8000` by default; if that port is already taken, check who owns it
before picking another (see the warning it prints), then update
`frontend/vite.config.js`'s proxy target to match:
```
$env:API_PORT = "8001"; python -m api.main
```

### Start the frontend dev server
**Purpose:** React/Vite UI that calls the API.
```
cd frontend
npm run dev
```
Other frontend scripts:
```
npm run build      # production build
npm run preview    # preview the production build locally
```

---

## Onboarding a new return (full pipeline)

### One command: build everything for a new return
**Purpose:** Runs the entire 7-step index pipeline (schema → embeddings → concept map →
concept embeddings → BM25), plus an optional 8th step to draft/validate/merge QA pairs.
```
python embedding_building/cims_raq_quarterly/build_new_return.py \
    --return-name "CIMS_X(Quarterly)" \
    --taxonomy data/x_taxonomy.json \
    --generate-qa 5
```
**Precondition (manual, must be true first):**
- The return's tables/columns are described in `data/.json-formatted`, every column
  tagged with `return_name` exactly matching `--return-name` above.
- The return's XBRL taxonomy export JSON is saved somewhere under `data/`.

**Flags:**
| Flag | Purpose |
|---|---|
| `--return-name` (required) | Exact return_name tag, e.g. `"CIMS_ALE_Domestic(Quarterly)"` |
| `--taxonomy` (required) | Path to the XBRL taxonomy export JSON |
| `--skip-db` | Skip Oracle DDL/constraint refresh, reuse what's already in `data/` |
| `--described-only` | Exclude live tables with no Excel description yet |
| `--with-member-index` | Also rebuild the member index (unused today, weight 0 — leave off) |
| `--generate-qa PER_TABLE` | Also draft+validate+merge QA pairs, N questions per table, and rebuild `qa_index.faiss` |

**Example — re-run against an already-onboarded return, DB already fresh, with QA generation:**
```
python embedding_building/cims_raq_quarterly/build_new_return.py \
    --return-name "CIMS_ALE_Domestic(Quarterly)" \
    --taxonomy data/2057.json \
    --skip-db \
    --generate-qa 5
```

---

## QA pairs (question → SQL examples)

### Add QA coverage to any return, standalone
**Purpose:** Draft candidate questions via Claude, validate each one (schema check, real
column check, `validate_sql`, and live Oracle execution requiring real non-zero rows),
merge only the survivors into `qa_pairs.json`, and rebuild `qa_index.faiss` — one command,
usable any time on any already-onboarded return, not just brand-new ones.
```
python embedding_building/cims_raq_quarterly/add_qa_pairs.py \
    --return-name "CIMS_X(Quarterly)" \
    --per-table 5
```
**Flags:** `--return-name` (required), `--per-table` (default 5), `--model` (default
`sonnet`), `--timeout` (default 120, seconds per table).

**Example:**
```
python embedding_building/cims_raq_quarterly/add_qa_pairs.py \
    --return-name "CIMS_ALE_Domestic(Quarterly)" \
    --per-table 5
```

### Draft QA pairs only (no validation, no merge)
**Purpose:** Just the drafting step, if you want to eyeball candidates before running
anything else. Writes a plain JSON list, does not touch `qa_pairs.json`.
```
python embedding_building/cims_raq_quarterly/generate_return_questions.py \
    --return-name "CIMS_X" \
    --per-table 5 \
    --out embedding_building/cims_raq_quarterly/qa_pairs_new.json
```
**Example:**
```
python embedding_building/cims_raq_quarterly/generate_return_questions.py \
    --return-name "CIMS_ALE_Domestic(Quarterly)" \
    --per-table 3 \
    --out embedding_building/cims_raq_quarterly/qa_pairs_new.json \
    --timeout 120
```

### Validate + merge drafted pairs manually
**Purpose:** The validation/merge step `add_qa_pairs.py` calls internally — useful to run
by hand for finer control (dry run first, then execute, then write).
```
python -m scripts.merge_qa_pairs                  # dry run: static checks only, no DB
python -m scripts.merge_qa_pairs --execute         # also runs each SQL against Oracle (still no write)
python -m scripts.merge_qa_pairs --execute --write # merges survivors into qa_pairs.json (backs up first)
```
Reads candidates from `{EMBEDDING_DIR}/qa_pairs_new.json` — the exact file
`generate_return_questions.py --out` should point at.

### Rebuild the QA index
**Purpose:** Re-embeds every question in `qa_pairs.json` into `qa_index.faiss`. Needed
after any manual edit to `qa_pairs.json` (the automated tools above already call this).
```
python embedding_building/cims_raq_quarterly/build_qa_index.py
```

---

## Individual build steps (usually run via `build_new_return.py`, but runnable alone)

### Refresh the Oracle DDL dump
**Purpose:** Pulls the whole schema's `CREATE TABLE` statements from `ALL_TAB_COLUMNS`.
```
python embedding_building/extract_schema.py
```

### Refresh constraints (PK/FK/UNIQUE)
**Purpose:** Pulls PK/FK/UNIQUE from Oracle. Whole-schema only — never pass a table
filter here, it overwrites rather than merges.
```
python embedding_building/extract_constraints.py
```

### Merge a return's tables into schema.json
**Purpose:** Scopes `data/schema.sql` + `data/.json-formatted` + live Oracle tables down
to one return, merges into `schema.json` by `return_name` (other returns untouched).
```
python embedding_building/cims_raq_quarterly/build_schema.py --return-name "CIMS_X(Quarterly)"
```
Add `--described-only` to exclude live-but-undescribed tables instead of synthesizing a
description for them.

### Rebuild table/column/row-label embeddings
**Purpose:** Reads `schema.json`, samples real row-label values from Oracle, builds the
three dense indexes.
```
python embedding_building/cims_raq_quarterly/build_embeddings.py
```

### Rebuild the XBRL concept map
**Purpose:** Distills one or more taxonomy JSON exports into `concept_map.json` (units,
stock/flow, row-code selectors, formulas). Pass every known return's taxonomy in one run
or earlier returns' data is dropped — `build_new_return.py` handles this automatically
via `taxonomies.json`; calling this directly does not.
```
python embedding_building/cims_raq_quarterly/build_concept_map.py --taxonomy data/2065\ 1.json --taxonomy data/2057.json
```

### Rebuild concept/member embeddings
**Purpose:** Builds `concept_index.faiss`/`member_index.faiss` and re-embeds
`table_index.faiss` with XBRL-enriched text (overwrites the file `build_embeddings.py` wrote).
```
python embedding_building/cims_raq_quarterly/build_concept_embeddings.py
```

### Rebuild the BM25 lexical index
**Purpose:** Builds `bm25_table_index.pkl` for exact-term matching (e.g. "Part A" vs "Part B").
```
python embedding_building/cims_raq_quarterly/build_bm25_index.py
```

---

## Validation (run after editing hand-authored files)

### Validate business_dictionary.yaml
**Purpose:** Checks every alias target is a real table, no alias steals another table's
traffic, and no surface form is registered twice with different targets. Run after every
edit to `embedding_building/business_dictionary.yaml`.
```
python scripts/validate_business_dictionary.py
```

### Check row-label keyword coverage
**Purpose:** Live Oracle cardinality probe — finds VARCHAR columns that look categorical
but aren't covered by the row-label keyword list in `src/description_fetcher.py`. Reports
candidates only, never auto-registers anything.
```
python -m scripts.validate_label_coverage
```

### Check Oracle connectivity
**Purpose:** Confirms `.env`/config DB credentials work, prints live table counts.
```
python embedding_building/cims_raq_quarterly/check_db_connection.py
```

---

## Evaluation / accuracy testing

### Full accuracy benchmark
**Purpose:** Runs every question in `table_qa.json` through the real pipeline
(retrieval → SQL generation → execution) and scores it against hand-written gold SQL.
```
python -m eval.run_eval                              # full run, with DB execution
python -m eval.run_eval --no-exec                     # skip DB execution, retrieval/validity only
python -m eval.run_eval --table CIMS_RAQ_M_SEC1_PART_A_DOM
python -m eval.run_eval --limit 20
```
Results land in `eval/results/run_<timestamp>.json` and `eval/results/latest_summary.md`.

### Retrieval-only benchmark (no LLM, no DB)
**Purpose:** Fast (~17s for 100 questions) retrieval accuracy check — top1/hit@k/MRR —
used to re-tune fusion weights (`BM25_SIGNAL_WEIGHT`, `HYBRID_BLEND_GAMMA`, etc.) in
`src/config.py`.
```
python -m scripts.eval_retrieval --dataset table_qa
python -m scripts.eval_retrieval --dataset qa_pairs --limit 50
python -m scripts.eval_retrieval --json results/retrieval_run1.json
python -m scripts.eval_retrieval --compare results/retrieval_run1.json   # diff against a prior run
```

### Offline regression guard suite
**Purpose:** Hand-verified real failure cases (from `eval/results/hallucination_log.jsonl`)
re-checked with no DB and no LLM — safe as a pre-commit/CI gate.
```
python -m scripts.test_accuracy_guards
```

### Scenario tests for RAQ-specific behavior
**Purpose:** Targeted tests for known tricky cases (vertical tables, dom/ove pairs, etc).
```
python -m scripts.test_raq_scenarios
python -m scripts.test_raq_scenarios --with-llm    # also exercises the real Ollama call
```

### Live end-to-end verification
**Purpose:** Run this only where Ollama and Oracle are actually reachable — checks
whether the first generation attempt is empty, how many tables reach the SQL model, and
whether the corrected SQL executes and returns rows.
```
python -m scripts.verify_live
python -m scripts.verify_live "your own question here" "and another"
```

### Unit-conversion spot check
**Purpose:** Verifies the taxonomy-declared unit multipliers (e.g. Rs lakh) are applied correctly.
```
python -m scripts.verify_unit_conversion
```

### XBRL reconciliation check
**Purpose:** Cross-checks the concept map's row-code selectors against the real taxonomy structure.
```
python -m scripts.phase0_xbrl_reconcile
python -m scripts.phase0_xbrl_reconcile --no-db              # skip live DB validation
python -m scripts.phase0_xbrl_reconcile --json report.json   # also write the full report as JSON
```
