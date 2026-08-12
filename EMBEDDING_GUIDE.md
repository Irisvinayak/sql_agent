# Embedding Guide — Building the Retrieval Index

How to build or extend the embedding indexes that `src/retriever.py` reads at query time. Everything here operates on **one** live directory: `embedding_building/cims_raq_quarterly/` (the default `EMBEDDING_DIR` in `src/config.py`). Every return (RAQ, ALE, and any future one) lives together in that single folder's `schema.json`, distinguished only by a `return_name` tag — there is no separate folder per return.

---

## Workflow

```
PHASE 1 — Tell the system the new return exists        (manual, one-time)
  1. Describe its tables/columns in data/.json-formatted,
     tagging every column with the exact return_name you'll use below.
  2. Save its XBRL taxonomy export JSON somewhere under data/.

PHASE 2 — Rebuild the "brain" that finds the right table   (one command, below)

  schema.json  ->  dense indexes  ->  concept_map.json  ->  concept indexes  ->  BM25 index
  "what tables     "what columns      "what business        "match by            "match by
   exist"           mean, sampled      concept each           meaning /            exact
                    row-labels"        table maps to"         XBRL text"           words"

PHASE 3 — Teach it good examples                        (manual, after)
  3. Draft question -> SQL examples for the new return with
     generate_return_questions.py (below), review them against the live
     Oracle schema, then merge the good ones into qa_pairs.json and rebuild
     qa_index.faiss.
  4. Add the return's real table joins to semantic_layer.yaml.
  5. Add the return's acronyms / section aliases to business_dictionary.yaml.
  6. Restart the API process — indexes are cached in memory for its lifetime.
```

Phase 2 is fully automated by `build_new_return.py`. Phases 1 and 3 stay manual on purpose — they need a human to read the actual return and write real examples and real join relationships. Skipping Phase 3 is the single most common cause of a newly-onboarded return retrieving worse than a mature one: QA examples are the highest-weighted retrieval signal (`QA_SIGNAL_WEIGHT=2.5` in `src/config.py`).

---

## Run command

```
python embedding_building/cims_raq_quarterly/build_new_return.py \
    --return-name "CIMS_X(Quarterly)" \
    --taxonomy data/x_taxonomy.json
```

That single command runs, in order, stopping at the first failure:

| # | Script | Rebuilds | Reads |
|---|--------|----------|-------|
| 1 | `extract_schema.py` | `data/schema.sql` from live Oracle DDL | Oracle |
| 2 | `extract_constraints.py` | `data/constraints.json` (whole schema, never filtered — it overwrites rather than merges, so a filtered run silently erases every other return's keys) | Oracle |
| 3 | `build_schema.py --return-name ...` | `schema.json` — merges the new return in, leaves other returns' entries untouched | `data/schema.sql`, `data/.json-formatted`, `data/constraints.json`, live Oracle table list |
| 4 | `build_embeddings.py` | `table_index.faiss`, `column_index.faiss`, `row_label_index.faiss`, `description_samples.json`, `needs_trim.json` | `schema.json`, Oracle (for row-label sampling) |
| 5 | `build_concept_map.py` | `concept_map.json`, from every known taxonomy (tracked in `taxonomies.json` so earlier returns are never dropped on a later rebuild) | taxonomy JSON(s) |
| 6 | `build_concept_embeddings.py` | `concept_index.faiss`, `member_index.faiss`, re-embeds `table_index.faiss` with XBRL-enriched text | `concept_map.json`, `schema.json` |
| 7 | `build_bm25_index.py` | `bm25_table_index.pkl` — imports `build_concept_embeddings.py`'s module directly for its enriched table documents | `schema.json`, `concept_map.json` |

### Useful flags

- `--skip-db` — skip steps 1–2 and reuse the `data/schema.sql` / `data/constraints.json` already on disk (faster while iterating).
- `--described-only` — passed through to `build_schema.py`; excludes live tables that have no Excel description yet instead of including them with a synthesized description.

### After it finishes

```
python embedding_building/cims_raq_quarterly/build_qa_index.py   # after adding QA pairs
python scripts/validate_business_dictionary.py                    # after adding aliases
```

Then restart the API — `src/retriever.py` caches every FAISS index and `schema.json` for the life of the process, so none of the rebuilt files are visible until it restarts.

---

## Drafting QA pairs for a return (Phase 3, step 3)

`generate_return_questions.py` drafts synthetic question/SQL pairs for a return's
tables by calling the `claude` CLI (`claude -p`, no separate API key needed — it
reuses whatever Claude access is already configured on the machine). For each
table matching `--return-name`, it feeds Claude that table's columns/description
from `schema.json` plus its row-label sample values from `description_samples.json`,
and asks for realistic user questions with the Oracle SQL that answers each one.

```
python -m embedding_building.cims_raq_quarterly.generate_return_questions \
    --return-name "CIMS_BDA" \
    --per-table 3 \
    --out embedding_building/cims_raq_quarterly/bda_qa_pairs.json
```

- `--return-name` — case-insensitive substring match against each table's
  `return_name` in `schema.json` (e.g. `"CIMS_BDA"` catches every BDA table
  without needing the exact `"CIMS_BDA_Something(Quarterly)"` string).
- `--per-table` — how many questions to draft per table (default: 5).
- `--out` — output path (default: `<return-slug>_qa_pairs.json` in this folder).
- `--model` — model alias passed to `claude --model` (default: `sonnet`).

**This is an offline authoring aid, not an automated merge.** Claude never sees
live row data or the real Oracle schema — only the `schema.json` columns and the
sampled label values already captured in `description_samples.json` — so it can
occasionally reference a column that isn't actually on that table. Always:

1. Review every generated pair against the live Oracle schema (or `data/schema.sql`).
2. Copy the good ones into `qa_pairs.json`.
3. Rebuild the QA index: `python embedding_building/cims_raq_quarterly/build_qa_index.py`.

If a table has no entry in `description_samples.json` yet, questions for it will
skip label-literal filters — nothing breaks, there's just less variety.

---

## Individual scripts (if you need to run one manually)

| Script | Standalone usage |
|---|---|
| `check_db_connection.py` | Verifies `src/config.py`'s `DB_*` credentials are correct and reachable; also lists live tables matching a prefix. |
| `build_schema.py` | `python embedding_building/cims_raq_quarterly/build_schema.py --return-name "CIMS_RAQ(Quarterly)"` — parses DDL + Excel descriptions, cross-checks against live Oracle, writes `schema.json`. Flags tables whose columns disagree on `return_name`, and live tables with no Excel description at all. |
| `build_embeddings.py` | No args. Reads `schema.json`, samples real row-label values from Oracle, builds table/column/row-label indexes. |
| `build_concept_map.py` | `--taxonomy <path>` (repeatable — pass every return's taxonomy you want kept, since this script only keeps what you pass it in one run). |
| `build_concept_embeddings.py` | No required args. `--with-member-index` also rebuilds the member index (currently unused at query time, `MEMBER_SIGNAL_WEIGHT=0`). |
| `build_bm25_index.py` | No args. |
| `build_qa_index.py` | No args. Reads `qa_pairs.json`, writes `qa_index.faiss`. |

---

## Historical notes

**Why `CIMS_RAQ(Quarterly)` was the first scoped build.** `data/.json-formatted` (the Excel-sourced description source of truth) has no `"CIMS_RAQ(Annual)"` return at all — `CIMS_RAQ(Quarterly)` was picked as the real, well-covered return (all tables live in Oracle) to validate the pipeline against before scaling to more returns.

**Two real bugs this build surfaced, both fixed at the source:**

1. Row-label sampling `.strip()`s values before storing them, but several tables' actual DB values have leading whitespace (e.g. `'     C2. Slipped to NPAs'`). An exact `=` filter using the stripped sample silently returned zero rows. Fixed by using `WHERE TRIM(col) = '...'` for label-column filters generated from sampled values.
2. `formatter.py::load_descriptions()` was reading field names (`db_name`/`excel_name`) that don't exist in the real `data/.json-formatted` file (the actual fields are `column_name`/`column_Description`). This meant the function silently returned `{}` on every run — **no Excel-sourced description had ever actually been merged into any schema.json build**, including the production one, until this was fixed at the source.

**ALE onboarding status.** The `CIMS_ALE_Q_*` tables are in `schema.json` and their dense/BM25 indexes are current, but `qa_pairs.json` has zero ALE examples and `business_dictionary.yaml`/`semantic_layer.yaml` have zero ALE entries — Phase 3 above was not completed for ALE. See `report/ALE_readiness_report.md` (local analysis, not committed) for the full per-subsystem status.
