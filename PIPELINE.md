# Pipeline — Module Reference, History, Evaluation

Companion to [ARCHITECTURE.md](ARCHITECTURE.md) (diagrams) and [EMBEDDING_GUIDE.md](EMBEDDING_GUIDE.md) (build pipeline). This document is the module-by-module reference, a record of resolved issues worth remembering, and how accuracy is measured.

---

## 1. Request flow, step by step

```
Step 1 — POST /api/query  { "query": "Show NPA for latest quarter" }

Step 2 — Guards (api/routes/query.py)
         length >= 20 chars; soft hint if no time expression present

Step 3 — Embed once (src/retriever.py::compute_query_embedding)
         business_dictionary.expand_acronyms(query) -> bge-large-en -> one 1024-d vector,
         reused by every signal below (no re-embedding per signal)

Step 4 — Exact-match check (src/retriever.py::find_exact_qa_match)
         literal similarity >= 0.99 against a stored qa_pairs.json question?
         YES -> frequency-resolve the matched table (Step 7b, same logic) against the
         replayed SQL's table name, then execute that SQL directly, skip everything
         below, source=direct_match

Step 5 — Retrieval (src/retriever.py::get_relevant_schema)
         7 signals run concurrently, fused by RRF:
           qa 2.5 · table 2.0 · concept 2.0 · column 1.5 · bm25 1.5 · row_label 1.0 · member 0.0 (disabled)
         + hybrid-blend calibration (gamma 0.3) + QA-match bonus + relative-floor prune
         + hard overrides, in order: strong-QA-match -> alias pin -> explicit section
           reference -> domain gate (src/context/domain.py::build_gate — explicit Part
           A/B/C/D reference, narrows-only-if-non-empty)
         the few-shot `qa_example` surfaced for the prompt is then restricted to the
         table ranked #1 after all of the above — never a different table's example,
         even one with a higher raw text-similarity score

Step 6 — Selection (src/selector.py::select_tables) — DETERMINISTIC, no LLM call
         short-circuits: strong_match / <2 candidates / top dominates runner-up by >=2x
         else: a declared join (semantic_layer.yaml) may add a second table, else top-1

Step 7 — Prune columns/labels of rejected tables

Step 7b — Frequency resolution (src/frequency_alias.py::resolve_frequency_table)
         question names a reporting cadence explicitly (monthly/quarterly/daily/
         fortnightly/annual)? -> swap the selected table for its real sibling at that
         cadence, discovered dynamically from the live Oracle catalog (no per-return
         table-naming assumption). No sibling exists -> keep the selected table,
         surface a warning. Applied on both the exact-match path (Step 4) and here.

Step 8 — Generate (src/sql_generator.py::generate_sql)
         build_prompt(): static rules + DDL + business semantics + time-context + one QA example
         -> Ollama (sqlcoder-7b-2), streamed
         -> validate_sql() + dry_run_sql() (Oracle EXPLAIN PLAN)
         up to 3 correction retries, with failed-attempt memory and a same-SQL early exit

Step 9 — Post-generation advisory checks
         check_stock_aggregation() (gated by BUSINESS_SEMANTICS_LEVEL)
         check_literal_validity() (always on)

Step 10 — Execute (src/executor.py::execute_query)
          pooled Oracle connection, fetchmany(DB_MAX_ROWS)

Step 11 — Respond: sql, rows, warnings, source, timings_ms
```

---

## 2. Module reference

### `src/config.py`
Central settings, all environment-driven (see `.env.example`). Every other module reads `config.<NAME>` at call time, not via a frozen import, so an env override takes effect without a code change. `EMBEDDING_DIR` defaults to `embedding_building/cims_raq_quarterly` — the live, actively-maintained build. `DB_HOST`/`DB_USER`/`DB_PASSWORD` deliberately have no fallback (an earlier version of this codebase committed live credentials directly in source).

### `src/vectorizer.py`
Wraps `sentence-transformers` (`BAAI/bge-large-en`, 1024-d) and FAISS. Documents are embedded without a prefix; queries use BGE's required asymmetric prefix (`QUERY_PREFIX`). `IndexFlatIP` on L2-normalized vectors gives exact cosine similarity.

### `src/retriever.py`
Runs 7 signals concurrently in a thread pool: table/column/row-label dense search, QA (prior-question) match, XBRL concept match, dimension-member match (disabled, weight 0), and BM25 lexical search. Fuses them by reciprocal-rank fusion, then applies an additive hybrid-blend calibration term and a QA-match bonus, prunes by a relative floor, and finally applies hard overrides (strong QA match, business-dictionary alias pin, explicit section reference, domain gate — see `src/context/domain.py`) that outrank the fused score entirely. FAISS indexes and metadata are cached at module scope for the life of the process — a rebuilt index needs a process restart to take effect.

### `src/context/domain.py`
`build_gate(query)` narrows the candidate table set using, in order: an explicit section reference (delegates to `src/section_alias.py`), an explicit Part A/B/C/D reference (`detect_parts`), and inferred periodicity wording (`detect_periodicity` — quarterly/monthly/annual/daily/fortnightly). Each layer only narrows if something survives — it can never empty the candidate set. Called from `src/retriever.py::get_relevant_schema` as the final hard override, after every other tier. `detect_periodicity()` is also reused directly by `src/frequency_alias.py` rather than re-implemented there.

### `src/frequency_alias.py`
Resolves a selected table to its sibling at a DIFFERENT reporting frequency when the question names one explicitly (e.g. "monthly sensitivity to securitization Part A" -> `CIMS_RAQ_M_SEC9_SENSEC_PARTA`, even though every embedding/QA pair was authored only against the quarterly tables). Sibling relationships are discovered dynamically from the live Oracle catalog (`src/executor.py::cached_accessible_tables`) — two tables are siblings if their names differ in exactly one underscore-token drawn from a small frequency vocabulary (Q/M/D/F/A and their spelled-out forms), with a guard against false positives like `PART_A`/`PART_B`/`PART_C`. This means a new return family gets frequency support automatically the moment its tables exist in Oracle — no per-return table-naming regex. No sibling for the requested frequency -> the originally selected table is kept and a warning is surfaced in the response.

### `src/selector.py`
Deterministic. **Makes no LLM call and no network call at all** — an earlier version routed through Ollama (`qwen2.5-coder:7b`) but was replaced because a single call cost 75–135s+. Short-circuits on a strong match, fewer than 2 candidates, or a dominant top score (>=2x the runner-up); otherwise a declared join from `semantic_layer.yaml` may pair two tables, or it falls back to top-1.

### `src/sql_generator.py`
Builds the LLM prompt (static rules, DDL for the selected table(s), business-semantics block, resolved time context, one worked QA example), calls Ollama, and validates the result. The correction-retry loop runs up to 3 rounds: temperature bumps modestly on retry, only the final round gets an extra bump, and a verbatim-repeat SQL triggers an immediate early exit instead of burning the remaining budget. `validate_sql()` hard-blocks non-SELECT statements, banned DML/DDL keywords, hallucinated tables/columns, and undeclared joins (when `semantic_layer.yaml` declares the pair); it does not check predicate/filter logic beyond string-literal equality.

### `src/executor.py`
Pooled Oracle connection (`oracledb`, thin mode, `min=2, max=10`). `dry_run_sql()` runs a real `EXPLAIN PLAN` before execution as an additional correctness gate beyond regex validation. `execute_query()` caps result rows at `DB_MAX_ROWS` (default 100) and never raises — DB errors are returned as an error string in the response.

### `src/description_fetcher.py`
Samples distinct values from label-shaped text columns (e.g. `PERIOD_DELINQUENCY`, `INDUSTRY_NAME`) directly from Oracle, so the LLM sees real row-label strings instead of guessing. Only text/label columns are sampled — no numeric or date column ever gets sample values.

### `src/business_dictionary.py`
Loads `embedding_building/business_dictionary.yaml` — acronym expansion (`npa` → `non performing assets`), synonym handling, and alias pins that route a phrase straight to one table, outranking embedding similarity entirely.

### `src/concept_map.py`, `src/business_semantics.py`
Read `concept_map.json` (the XBRL business-concept layer) and render it into the generation prompt, gated by `BUSINESS_SEMANTICS_LEVEL` (cumulative: `off` → `units` → `metrics` → `aggregation` → `dimensions` → `derivation`). `check_stock_aggregation()` warns when a point-in-time balance column is summed across dates without being pinned to one period.

### `src/literal_validator.py`
`check_literal_validity()` cross-checks WHERE-clause string-literal equality filters against `description_samples.json`'s real sampled values, warning with close-match suggestions when a literal isn't real. Advisory only — coverage is limited to columns with sampled labels.

### `api/routes/query.py`
The one production endpoint, `POST /query`. Orchestrates every module above in the order shown in §1. Wraps only `generate_sql`'s failure as an HTTP 502; other unexpected exceptions propagate as a 500.

### `api/routes/health.py`
`GET /health` (liveness) and `GET /test-db` (Oracle connectivity, diagnostic/manual use only — not called by the frontend).

---

## 3. Resolved issues (institutional memory)

These fixes shaped why the code looks the way it does today — recorded so a future change doesn't accidentally reintroduce them.

- **QA-bonus prune-floor bug** — the relative floor used to be computed *after* the QA-match bonus was applied, which could inflate the reference point every other table's floor was measured against, silently dropping a genuinely relevant table. Fixed by snapshotting the pre-bonus top score; kept behind a `QA_BONUS_FLOOR_FIX` kill-switch (default on) for rollback safety.
- **`check_stock_aggregation()` false negative** — the "pinned to one period" check used to match the bare substring `MAX(RDATE)` regardless of the comparison operator, so `RDATE <= (SELECT MAX(RDATE)...)` (which spans all history) was wrongly treated as pinned. Fixed to require an actual `=`/`BETWEEN` relationship.
- **Correction-retry loop could repeat the same mistake for all 3 rounds** — retry prompts had no memory of earlier attempts. Fixed with failed-attempt memory, a same-SQL-twice early exit, and a temperature bump reserved for the final round only (a flat bump across all rounds was tried and rejected).
- **Row-label sampling stripped values that Oracle stores with padding** — `description_fetcher.py` used to `.strip()` sampled values, but several real DB values have leading whitespace (e.g. `'     C2. Slipped to NPAs'`). An exact `=` filter using the stripped sample silently returned zero rows. Any prompt drawing on sampled row-labels should use `TRIM()` for label-column filters.
- **Excel-sourced descriptions were never actually merged into any schema build** — `formatter.py::load_descriptions()` read field names (`db_name`/`excel_name`) that don't exist in the real source file (the actual fields are `column_name`/`column_Description`), so it silently returned `{}` on every run. Fixed at the source; this benefited every schema build going forward, not just the one that surfaced it.
- **The selector used to be an LLM call** — replaced with the deterministic logic in `src/selector.py` after repeated 75–135s+ latencies and outright 404s against the configured Ollama proxy made it unreliable. See `src/selector.py`'s own module docstring for the full reasoning.
- **`build_gate()` was written but never called** — `src/context/domain.py::build_gate` already implemented explicit-section/explicit-part/inferred-periodicity narrowing, but no module in the live request path (`retriever.py`, `selector.py`, `api/routes/query.py`) ever invoked it. Concretely: "sensitivity to securitization Part A" scored `cims_raq_q_sec9_sensec_PARTB` (0.78) above the correct `..._PARTA` (0.631) on embedding similarity alone — the explicit "Part A" in the question had zero effect on table selection. Fixed by calling `build_gate()` from `get_relevant_schema()` as the final override tier, after strong-QA-match/alias/section-reference.
- **Monthly/daily/fortnightly questions had no path to the right table at all** — every embedding/QA pair was authored only against quarterly RAQ/ALE tables, and building a full second embedding set per cadence would mean re-authoring near-identical questions for tables whose schema is already known to be identical (ALE) or a documented subset (RAQ). Solved with `src/frequency_alias.py`'s dynamic, catalog-driven sibling resolution instead of a second embedding build or a per-return table-naming regex — see that module's entry above.
- **The few-shot `qa_example` could belong to a completely different table than the one actually selected for generation** — it was chosen purely by global question-text similarity across ALL of `qa_pairs.json`, with no check against the table retrieval/selection actually settled on. Real case: "sensitivity to securitization Part A ... monthly" selected `CIMS_RAQ_Q_SEC9_SENSEC_PARTA` but the injected worked example was for `CIMS_RAQ_Q_SEC1_PART_A_DOM` (real columns `PERIOD_DELINQUENCY`, `TOTAL_LOAN_ASSETS` — nothing to do with the selected table), and the model duly hallucinated a JOIN plus invented columns blending both tables. Fixed in `get_relevant_schema()` to only surface a `qa_example` whose table matches `tables[0]` (the table the selector will almost always pick — `select_tables()` returns top-1 except for a declared join); if no qualifying example exists for that table, no example is shown at all, which is safer than showing a wrong-table one.

---

## 4. Known performance characteristics

- Every stage is timed with `time.perf_counter()` and surfaced in the response as `timings_ms` (see `TimingsPanel` in the frontend).
- The dominant cost in a slow request is the SQL-generation correction-retry loop: up to 1 initial call + 3 retries, each individually allowed a 300s read timeout with no cross-round wall-clock budget — a slow-but-responsive backend can consume several minutes on one request in the worst case.
- Retrieval's 7 signals run concurrently (not sequentially) via a thread pool.
- `schema.json` and every FAISS/BM25 index are cached at module scope after first load — not re-read per request.
- There is no statement-level Oracle timeout configured on the connection pool today.

---

## 5. Evaluation

Two complementary harnesses:

- **`python -m eval.run_eval`** — benchmarks the full pipeline (retrieval, SQL validity, DB execution, execution-match accuracy) against hand-written gold pairs in `table_qa.json`. Use `--no-exec` to skip DB execution when Oracle isn't reachable, `--table <name>` to scope to one table, `--limit N` for a quick smoke run. Each run writes `eval/results/run_<timestamp>.json` (full per-question detail) and `eval/results/latest_summary.md` (aggregate + per-table breakdown, overwritten each run).
- **`python -m scripts.eval_retrieval`** — retrieval-only (no LLM, no DB needed): top1/top3/hit@k/MRR/column-recall against `eval/raq_user_queries.json` (the leak-free, recommended dataset).

**Diagnostic rule:** if `sql_validity_rate` is high but `execution_success_rate` is low, the generated SQL is well-formed against `schema.json` but `schema.json` itself has drifted from the real Oracle table — check `schema.json` against the live DB (`embedding_building/extract_schema.py`) before touching the model or prompts.

To extend the ground truth, add more `{question, sql}` pairs to `table_qa.json`, especially for tables scoring low in `latest_summary.md`.

For a full accuracy gap analysis (what's measured, what isn't, ALE coverage status), see `report/accuracy_report.md` and `report/ALE_readiness_report.md` (local analysis output, not committed).
