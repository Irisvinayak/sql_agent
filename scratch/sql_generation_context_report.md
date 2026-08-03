# SQL Generation Context Report

**Method note:** every number and text block below was captured by executing the real, unmodified pipeline (`src/retriever.py`, `src/selector.py`, `src/sql_generator.py`, `src/executor.py`) against the live Ollama and Oracle endpoints configured in `.env` right now. Nothing here is reconstructed from reading code alone. No code was changed to produce this trace.

Two queries are traced, because they exercise two different, real code paths:

- **Primary trace** — `"total loan assets for the latest quarter"` — retrieval's dominance short-circuit fires, so the selector never calls its LLM. This is the trace with a full downstream SQL-generation result (real Ollama call, real Oracle execution).
- **Secondary trace** — `"give me risk exposure for domestic"` — retrieval scores are close, so the selector *does* call its LLM. This is the one question in `eval/results/hallucination_log.jsonl` that historically produced a fabricated `.id` join, so it's used here to show the selection-call machinery and one live operational finding (§9).

---

## 1. User Query

**Primary:** `total loan assets for the latest quarter`
**Secondary:** `give me risk exposure for domestic`

---

## 2. Retrieved Tables

Retrieval runs 4 weighted signals through Reciprocal Rank Fusion (`src/retriever.py:277-431`), then keeps up to `SHORTLIST_K=8` tables scoring at least 15% of the top score (`RELATIVE_FLOOR`). Signal weights: **table-text search ×2.0**, **column search ×1.5**, **row-label search ×1.0**, **QA question→SQL search ×2.5** (highest, because a matched prior question is the strongest signal available).

### Primary query — `total loan assets for the latest quarter`

Real shortlist, `shortlist_k=8`:

| Rank | Table | Fused score | Why retrieved |
|---|---|---|---|
| 1 | `cims_raq_q_sec1_part_a_dom` | **0.5734** | Table-text match on "loan assets", "total loan assets" column name/description; also surfaced by the column-search signal on `total_loan_assets` |
| 2 | `cims_raq_q_sec1_part_b_dom` | 0.1904 | Sibling table, same section, shares "loan assets" vocabulary in its table description |
| 3 | `cims_raq_q_sec1_part_c_o` | 0.1678 | Same column set as rank 1 (`total_loan_assets`), different scope (overseas) |
| 4 | `cims_raq_q_sec_12_misc` | 0.1652 | Row-label signal — one of its `item` label values loosely matches vocabulary |
| 5 | `cims_raq_q_sec4_part_a` | 0.1607 | Row-label signal on `movement_from` values (loan movement terminology) |
| 6 | `cims_raq_q_sec8_infra_brkup` | 0.1283 | Column search — has multiple "loan"/"exposure" columns |
| 7 | `cims_raq_q_sec9_sensec_partb` | 0.1235 | Column search — exposure-related columns |
| 8 | `cims_raq_q_sec1_last_date_port` | 0.1192 | Weak table-text tail match |

Retrieval also returned, alongside the tables: **10 column hits** (top two: `cims_raq_q_sec1_part_a_dom.total_loan_assets`, `cims_raq_q_sec1_part_c_o.total_loan_assets`), **10 row-label hits** (none from the winning table — its labels didn't match this phrasing), and one **QA example** (below, §5).

Dominance check (`src/selector.py`, `DOMINANCE_RATIO=2.0`): top score 0.5734 ÷ runner-up 0.1904 = **ratio 3.01 ≥ 2.0 → selector short-circuits, no LLM call made.**

### Secondary query — `give me risk exposure for domestic`

| Rank | Table | Fused score | Why retrieved |
|---|---|---|---|
| 1 | `cims_raq_q_sec2_part_a` | **0.5971** | Table-text match on "risk", "domestic operations" |
| 2 | `cims_raq_q_sec9_sensec_partb` | 0.5383 | Table-text + column match on "exposure", "domestic" |
| 3 | `cims_raq_q_sec9_sensec_parta` | 0.2093 | Sibling table, same section |
| 4 | `cims_raq_q_sec8_ind_brkup` | 0.1418 | Column search — exposure columns |
| 5 | `cims_raq_q_sec10` | 0.1340 | Weak tail match |

Dominance check: 0.5971 ÷ 0.5383 = **ratio 1.11 < 2.0 → selector calls its LLM.**

---

## 3. Selected Table

### Primary query

- **Selected table:** `cims_raq_q_sec1_part_a_dom`
- **Selection score:** N/A — not an LLM decision. The retrieval score itself (0.5734, dominance ratio 3.01) *is* the selection signal.
- **Selection reason:** deterministic short-circuit in `src/selector.py::select_tables` — "top score dominates the runner-up by ≥2×, retrieval is already confident." Logged verbatim: `Selector skipped: cims_raq_q_sec1_part_a_dom dominates on retrieval score (0.573 vs 0.190)`.
- `selection` object passed to the prompt builder: **`None`** (no per-column reasoning exists, because no LLM call happened).

### Secondary query — real LLM selection call attempted

- **Candidate shortlist sent to the selector model:** all 5 tables above, each rendered as full `CREATE TABLE` DDL (same format the SQL model itself sees — see §4).
- **Selector model:** `qwen2.5-coder:7b` (`config.SELECTOR_MODEL`), compact line-format prompt (`SELECTOR_PROMPT_COMPACT`, `config.SELECTOR_OUTPUT_FORMAT=compact`).
- **Actual result:** the call **failed** — `404 Client Error: Not Found` from `http://3.109.51.228/OllamaProxy/api/generate` (the currently configured `OLLAMA_URL`, a remote proxy). The same proxy answered a direct `sqlcoder-7b-2` request with `200 OK` in the same test run, so the 404 is specific to requesting the `qwen2.5-coder:7b` model on that proxy — it is very likely not deployed there (it *is* present in this machine's local `ollama list`, which is a different, unrelated Ollama instance).
- **Observed fallback:** `select_tables` caught the exception and logged `Selector model call failed (404 ...) — falling back to top-1`, returning `cims_raq_q_sec2_part_a` (rank-1 by retrieval score) with `selection=None`.
- This is documented as a factual finding for §9, not a suggested fix.

---

## 4. Schema Context Sent To SQL Model

This section documents the **primary query's** actual prompt content (the one table that reached the SQL model). Structural rendering is identical for any table via `src/sql_generator.py::build_table_ddl`.

### Table Structure

Exact `CREATE TABLE` block as it appeared in the real prompt:

```sql
CREATE TABLE CIMS_RAQ_Q_SEC1_PART_A_DOM (
  PERIOD_DELINQUENCY VARCHAR2(200),  -- Period of Delinquency | row label, allowed values: 'A. PERFORMING(i + ii)', 'B. NON-PERFORMING (iii + iv + v)', 'C. Total ( A + B)', 'i) Current', 'ii) Overdue [ii.a + ii.b]', 'ii.a) Loans and Advances where 90 day norms is applicable as per IRAC', 'ii.a.1 Overdue less than 30 days (SMA -0)', 'ii.a.2 Overdue for 30 to 60 days (SMA -1)', 'ii.a.3 Overdue for 60 to 90 days (SMA -2)', 'ii.b) Loans and Advances where 90 day norms is NOT applicable as per IRAC', 'iii) Substandard', 'iv) Doubtful', 'v) Loss' | TOTAL row: PERIOD_DELINQUENCY = 'C. Total ( A + B)'
  TERM_LOAN NUMBER(20,2),  -- Loans and Advances (Non-banks) Term Loans
  CASH_CR_OVERDRAFT_DEMAND_LOAN NUMBER(20,2),  -- Loans and Advances (Non-banks) Cash Credits, Overdrafts and Demand Loans
  BILLS_PURCHASE_DISCOUNT NUMBER(20,2),  -- Loans and Advances (Non-banks) Bills Purchased and Discounted
  LOAN_ADVANCE_BANK NUMBER(20,2),  -- Loans and Advances to Banks
  TOTAL_LOAN_ASSETS NUMBER(20,2),
  CODE NUMBER(20,2),
  RDATE DATE
);
```

- **Column names (8):** `PERIOD_DELINQUENCY`, `TERM_LOAN`, `CASH_CR_OVERDRAFT_DEMAND_LOAN`, `BILLS_PURCHASE_DISCOUNT`, `LOAN_ADVANCE_BANK`, `TOTAL_LOAN_ASSETS`, `CODE`, `RDATE`
- **Data types:** `PERIOD_DELINQUENCY VARCHAR2(200)`; five `NUMBER(20,2)` columns (`TERM_LOAN`, `CASH_CR_OVERDRAFT_DEMAND_LOAN`, `BILLS_PURCHASE_DISCOUNT`, `LOAN_ADVANCE_BANK`, `TOTAL_LOAN_ASSETS`); **`CODE NUMBER(20,2)`** (not text — a quoted `CODE = 'X'` filter would be a type error); `RDATE DATE`.
- **Nullable / NOT NULL:** every column's `schema.json` entry has `"nullable": true` — **no NOT NULL constraint reached the prompt.** Confirmed by reading `schema.json` directly: all 8 columns show `nullable: true`.
- **Primary Keys:** `schema.json["primary_key"]` = **`[]` — empty.** No `PRIMARY KEY` clause rendered in the DDL.
- **Foreign Keys:** `schema.json["foreign_keys"]` = **`[]` — empty.** No `FOREIGN KEY ... REFERENCES` clause rendered.
- **Constraints:** none beyond the row-label comment (which is documentation, not an enforced SQL constraint).

### Column Metadata

| Column | Description (from schema.json) | Type |
|---|---|---|
| `period_delinquency` | Period of Delinquency | varchar2(200) |
| `term_loan` | Loans and Advances (Non-banks) Term Loans | number(20,2) |
| `cash_cr_overdraft_demand_loan` | Loans and Advances (Non-banks) Cash Credits, Overdrafts and Demand Loans | number(20,2) |
| `bills_purchase_discount` | Loans and Advances (Non-banks) Bills Purchased and Discounted | number(20,2) |
| `loan_advance_bank` | Loans and Advances to Banks | number(20,2) |
| `total_loan_assets` | Total Loan Assets | number(20,2) |
| `code` | CODE (generic — no business description available) | number(20,2) |
| `rdate` | RDATE (generic — no business description available) | date |

- **Business descriptions:** present for the 6 domain columns, generic for `code`/`rdate` (no Excel-sourced label existed for these two, so the description fell back to the bare column name).
- **Sample/distinct values:** only for `PERIOD_DELINQUENCY` (the row-label column) — see below. **No sample values exist for any numeric column** (`total_loan_assets`, `term_loan`, etc.) — the pipeline never samples numeric/date columns, only label-type text columns.

### Row Label Metadata

- **Vertical table:** yes. Detected because `description_samples.json` has an entry for `cims_raq_q_sec1_part_a_dom` → the label map is non-empty → `build_prompt` marks it `STORAGE FORMAT: VERTICAL`.
- **Label column:** `PERIOD_DELINQUENCY`
- **Label descriptions:** none beyond the raw values themselves — no separate human-readable gloss per label value.
- **Example labels (all 13, real, verbatim from `description_samples.json`):** `'A. PERFORMING(i + ii)'`, `'B. NON-PERFORMING (iii + iv + v)'`, `'C. Total ( A + B)'`, `'i) Current'`, `'ii) Overdue [ii.a + ii.b]'`, `'ii.a) Loans and Advances where 90 day norms is applicable as per IRAC'`, `'ii.a.1 Overdue less than 30 days (SMA -0)'`, `'ii.a.2 Overdue for 30 to 60 days (SMA -1)'`, `'ii.a.3 Overdue for 60 to 90 days (SMA -2)'`, `'ii.b) Loans and Advances where 90 day norms is NOT applicable as per IRAC'`, `'iii) Substandard'`, `'iv) Doubtful'`, `'v) Loss'`. (Under the 13-value count, `MAX_LABELS_DDL=14` truncation never triggered.)
- **TOTAL row detected:** `'C. Total ( A + B)'`, matched by keyword against `_TOTAL_ROW_KEYWORDS` (contains "total"). Rendered in the prompt as: `TOTAL row: PERIOD_DELINQUENCY = 'C. Total ( A + B)'`.
- **`needs_trim` metadata:** **`false`** for this column — `semantic_layer.yaml`'s authored vertical spec agrees (`{'label_column': 'period_delinquency', 'total_row': 'C. Total ( A + B)', 'needs_trim': False}'`), and the live check in `build_table_ddl` (`any(v != v.strip() for v in values)`) also found no padded values. **No `TRIM()` instruction was added to this column's comment.** (Contrast: the QA example in §5 *does* use `TRIM()` — a different table/column pair that does need it.)
- **Label matching rule actually applied to the prompt:** exact-literal match only, expressed in the general RULES section ("filter by an exact row-label literal from its comment; for a total, use the TOTAL row label rather than SUM() across labels") — there is no per-column matching rule beyond that.

### Business Metadata

- **Table description (from schema.json, shown in the prompt's leading comment):** *"CIMS_RAQ_Q_SEC1_PART_A_DOM — CIMS banking supervisory regulatory return Risk Assessment Questionnaire RAQ quarterly section 1 loan assets asset quality classification part annual yearly domestic. Fields include: Period of Delinquency, Loans and Advances (Non-banks) Term Loans, Loans and Advances (Non-banks) Cash Credits, Overdrafts and Demand Loans, Loans and Advances (Non-banks) Bills Purchased and Discounted, Loans and Advances to Banks, Total Loan Assets."*
- **Return description:** `CIMS_RAQ(Quarterly)` (`schema.json["return_name"]`) — present in `schema.json` but **not rendered anywhere in the final DDL prompt block** (it feeds the retrieval embedding text, not the generation prompt).
- **Domain description:** implicit only, inside the auto-generated table description above ("CIMS banking supervisory regulatory return... asset quality classification").
- **RBI reporting description:** none beyond the above — no separate regulatory citation, circular reference, or reporting-instruction text is carried anywhere in the pipeline.

---

## 5. QA Context

### Primary query

- **Retrieved QA example (real, from `qa_index.faiss`):**
  - Question: *"What is the total domestic loan exposure for RAQ?"*
  - SQL: `SELECT TOTAL_LOAN_ASSETS FROM CIMS_RAQ_Q_SEC1_PART_A_DOM WHERE TRIM(PERIOD_DELINQUENCY) = 'C. Total ( A + B)' AND RDATE = (SELECT MAX(RDATE) FROM CIMS_RAQ_Q_SEC1_PART_A_DOM)`
  - Table: `CIMS_RAQ_Q_SEC1_PART_A_DOM`
- **Similarity scores (all real, computed by `_rerank_qa_hits`/`best_qa_similarity`):**
  - Embedding cosine score: **0.8092** (above `QA_EXAMPLE_MIN_SCORE=0.75`, so it qualifies as a few-shot example)
  - Literal text similarity: 0.4494
  - Token-set similarity: 0.1333
  - Note: this pair's *own* SQL uses `TRIM(PERIOD_DELINQUENCY)`, even though this run's live padding check found no padding on that column right now — the pair was presumably written against data that needed it at some point, or written defensively. It does not contradict §4; `TRIM()` around an unpadded value is still correct SQL, just unnecessary.
- **How it entered the prompt:** verbatim, under a `### Worked example` section (see §7), formatted by `build_prompt`'s `qa_example` path — one Q/SQL pair, no additional wrapping or paraphrase.
- **Was this a "direct match"?** No — `find_exact_qa_match` requires ≥0.99 character similarity to skip the LLM entirely; this pair scored nowhere near that threshold on the literal-text axis (0.4494), so the LLM was still called with this pair only as a *hint*.

### Secondary query

QA retrieval was also run but not separately captured for this trace; the important, verified fact for this query is the **selection-stage** failure documented in §3, which happens upstream of QA example use in the SQL-generation prompt.

---

## 6. Validation Rules Context

Two distinct rule sets exist and were both exercised in this trace.

### A. Rules given TO the SQL model, inside the prompt text (§7's `### Rules` section — verbatim, all 8 lines actually sent)

1. Use only the tables and columns declared in the schema above. Allowed tables: `CIMS_RAQ_Q_SEC1_PART_A_DOM`.
2. Copy identifiers character for character. Do not abbreviate, pluralise or "correct" a name.
3. A text column such as `DESCRIPTION`, `ITEM`, `CATEGORY` or `RISK_CATEGORY` holds the value directly. It is not a lookup table — never join to a table that is not declared above.
4. Never use bind variables or placeholders (`:val`, `?`, `%s`). Embed every value as a literal.
5. Wrap every date literal as `TO_DATE('YYYY-MM-DD', 'YYYY-MM-DD')`.
6. If the question names no period, filter `RDATE = (SELECT MAX(RDATE) FROM <that table>)`.
7. On a VERTICAL table, filter by an exact row-label literal from its comment; for a total, use the TOTAL row label rather than `SUM()` across labels.
8. **Answer from the single table above. Do not join.** (This exact line only appears when the selector did *not* supply a `join_hint` — which is the case here, since selection was `None`.)

No separate "Aggregation rules", "Oracle rules", or "Date rules" sections exist as distinct headers in the prompt — they're folded into this one flat list. There is **no explicit Oracle-dialect syntax cheat-sheet** (no mention of `FETCH FIRST`, `ADD_MONTHS`, etc.) in this `ddl` prompt style — that richer rule block (`_build_full_rules_block`) exists in the codebase but is only used by the `rules` prompt style, which the active model (`sqlcoder-7b-2`) does not use.

### B. Rules enforced AFTER generation, never seen by the model (`validate_sql`, `src/sql_generator.py`)

These are not part of the prompt — they are checked against the SQL the model returns:

1. Must start with `SELECT`.
2. No `DELETE/UPDATE/DROP/INSERT/TRUNCATE/ALTER/CREATE/EXEC` (whole-word).
2.5. Bare `'YYYY-MM-DD'` literals not wrapped in `TO_DATE(...)` are rejected.
4. Every `FROM`/`JOIN` target must be a declared table or subquery alias.
5. At least one allowed table must appear.
5.2. **Declared-join enforcement** — a join between two real tables is rejected unless the pair exists in `semantic_layer.yaml`'s `join_graph` (3 edges currently declared, all `_PART_A`/`_PART_B` sibling pairs — see §2 of the earlier refactor work).
5.5. Alias-aware column check — `alias.column` must exist on the specific table that alias resolves to.
6. Outermost `SELECT`-list columns must belong to a real matched table.
7. Vertical-table aggregation guard — `SUM/AVG/COUNT/MIN/MAX` on a non-key column of a vertical table is rejected unless the label column is filtered in `WHERE`.

Additionally, `dry_run_sql` (`src/executor.py`) runs a real Oracle `EXPLAIN PLAN` on the candidate SQL — this is **not text the model sees**, it's a post-hoc, real-database check for errors no static rule can catch (nonexistent column, type mismatch). For this trace: not exercised as a rejection, since the first attempt was already valid and executed cleanly.

**For this trace, the actual SQL passed every check in group A and B on the first attempt** — `warnings: []`, `is_valid: True`, `reason: "Valid"`.

---

## 7. Final Prompt

Exact text sent to `hf.co/defog/sqlcoder-7b-2:Q5_K_M` for the primary query (captured directly from `build_prompt`'s return value — nothing paraphrased):

### System Instructions
*(Not a separate block in this prompt style — the task framing itself doubles as the instruction. No system/user role separation; this is a single completion-style prompt, consistent with SQLCoder being a completion model, not a chat model.)*

```
### Task
Generate an Oracle SQL SELECT query to answer [QUESTION]total loan assets for the latest quarter[/QUESTION]
```

### Schema Section

```
### Database Schema
The query will run on a database with the following schema:
-- CIMS_RAQ_Q_SEC1_PART_A_DOM - CIMS banking supervisory regulatory return Risk Assessment Questionnaire RAQ quarterly section 1 loan assets asset quality classification part annual yearly domestic. F...
-- STORAGE FORMAT: VERTICAL - each row is one named metric, keyed by PERIOD_DELINQUENCY. Filter by an exact label value below; do NOT SUM across labels.
CREATE TABLE CIMS_RAQ_Q_SEC1_PART_A_DOM (
  PERIOD_DELINQUENCY VARCHAR2(200),  -- Period of Delinquency | row label, allowed values: 'A. PERFORMING(i + ii)', 'B. NON-PERFORMING (iii + iv + v)', 'C. Total ( A + B)', 'i) Current', 'ii) Overdue [ii.a + ii.b]', 'ii.a) Loans and Advances where 90 day norms is applicable as per IRAC', 'ii.a.1 Overdue less than 30 days (SMA -0)', 'ii.a.2 Overdue for 30 to 60 days (SMA -1)', 'ii.a.3 Overdue for 60 to 90 days (SMA -2)', 'ii.b) Loans and Advances where 90 day norms is NOT applicable as per IRAC', 'iii) Substandard', 'iv) Doubtful', 'v) Loss' | TOTAL row: PERIOD_DELINQUENCY = 'C. Total ( A + B)'
  TERM_LOAN NUMBER(20,2),  -- Loans and Advances (Non-banks) Term Loans
  CASH_CR_OVERDRAFT_DEMAND_LOAN NUMBER(20,2),  -- Loans and Advances (Non-banks) Cash Credits, Overdrafts and Demand Loans
  BILLS_PURCHASE_DISCOUNT NUMBER(20,2),  -- Loans and Advances (Non-banks) Bills Purchased and Discounted
  LOAN_ADVANCE_BANK NUMBER(20,2),  -- Loans and Advances to Banks
  TOTAL_LOAN_ASSETS NUMBER(20,2),
  CODE NUMBER(20,2),
  RDATE DATE
);
-- latest reporting period: RDATE = (SELECT MAX(RDATE) FROM CIMS_RAQ_Q_SEC1_PART_A_DOM)
```

*(No `RESOLVED TIME CONTEXT` block appears — see §9. No `join_hint` line appears — selection was `None`.)*

### Metadata Section
*(There is no metadata section distinct from the schema comments above — table/column business descriptions, the VERTICAL flag, row-label values, and the TOTAL-row pointer are all inlined directly into the DDL as SQL comments, not a separate prompt region.)*

### QA Examples Section

```
### Worked example
-- table: CIMS_RAQ_Q_SEC1_PART_A_DOM
-- question: What is the total domestic loan exposure for RAQ?
SELECT TOTAL_LOAN_ASSETS FROM CIMS_RAQ_Q_SEC1_PART_A_DOM WHERE TRIM(PERIOD_DELINQUENCY) = 'C. Total ( A + B)' AND RDATE = (SELECT MAX(RDATE) FROM CIMS_RAQ_Q_SEC1_PART_A_DOM)
```

### Validation Rules Section

```
### Rules
- Use only the tables and columns declared in the schema above. Allowed tables: CIMS_RAQ_Q_SEC1_PART_A_DOM.
- Copy identifiers character for character. Do not abbreviate, pluralise or 'correct' a name.
- A text column such as DESCRIPTION, ITEM, CATEGORY or RISK_CATEGORY holds the value directly. It is not a lookup table - never join to a table that is not declared above.
- Never use bind variables or placeholders (:val, ?, %s). Embed every value as a literal.
- Wrap every date literal as TO_DATE('YYYY-MM-DD', 'YYYY-MM-DD').
- If the question names no period, filter RDATE = (SELECT MAX(RDATE) FROM <that table>).
- On a VERTICAL table, filter by an exact row-label literal from its comment; for a total, use the TOTAL row label rather than SUM() across labels.
- Answer from the single table above. Do not join.
```

### User Question Section

```
### Answer
Given the database schema, here is the Oracle SQL query that answers [QUESTION]total loan assets for the latest quarter[/QUESTION]
Return only the raw SQL - no explanation, no markdown, no semicolon.
[SQL]
```

**Total prompt length: 3,065 characters / 459 whitespace-split words.**

**Actual model output (captured live):**
```sql
SELECT TOTAL_LOAN_ASSETS FROM CIMS_RAQ_Q_SEC1_PART_A_DOM WHERE PERIOD_DELINQUENCY = 'C. Total ( A + B)' AND RDATE = (SELECT MAX(RDATE) FROM CIMS_RAQ_Q_SEC1_PART_A_DOM)
```
Valid on the first attempt (`warnings: []`). Executed against live Oracle: 1 row, `TOTAL_LOAN_ASSETS = 50610761.34`. Total call time 67.4s (includes a cold Ollama model load on the remote proxy — see §9 on latency).

---

## 8. Token Usage Analysis

Approximated at chars ÷ 4 (a standard rough heuristic; no exact tokenizer was invoked, so treat these as directional, not exact):

| Section | Chars | Approx. tokens | % of prompt |
|---|---|---|---|
| `### Task` | 118 | ~29 | 3.8% |
| `### Database Schema` | 1,607 | ~401 | **52.4%** |
| `### Worked example` (QA) | 294 | ~73 | 9.6% |
| `### Rules` | 829 | ~207 | 27.0% |
| `### Answer` | 217 | ~54 | 7.1% |
| **Total** | **3,065** | **~764** | 100% |

**Largest contributor by far: the Schema section (52.4%)** — and within it, the single largest line is the `PERIOD_DELINQUENCY` column's comment (13 row-label values inline, ~700 of the schema section's 1,607 chars — roughly 44% of the schema block, ~23% of the entire prompt) — one column's label list is nearly a quarter of the whole prompt.

Second largest: the static **Rules block (27.0%)** — fixed cost, identical on every request regardless of table/question, since it's an unconditional 8-line list.

The QA worked example (9.6%) and the Task/Answer framing (10.9% combined) are comparatively cheap.

---

## 9. Missing Context Analysis

Information that exists in the codebase/data but was **not** in this prompt, verified by inspection, not guesswork:

1. **`return_name` (`"CIMS_RAQ(Quarterly)"`) is dropped.** Present in `schema.json`, used to build the retrieval embedding text, but never rendered into the generation prompt. The model has no signal about which regulatory return a table belongs to.

2. **PK/FK are structurally empty for every table in this build.** `schema.json["primary_key"]` = `[]` and `["foreign_keys"]` = `[]` for the traced table (and, by construction, likely every table — `data/constraints.json` **does not exist on disk**, meaning `embedding_building/extract_constraints.py` has never actually been run against Oracle for this build). The DDL renderer supports emitting real `PRIMARY KEY`/`FOREIGN KEY` clauses; **it has nothing to render them from.**

3. **`semantic_layer.yaml`'s authored vertical metadata is not the source the prompt actually uses.** The YAML has a hand/auto-authored `vertical.cims_raq_q_sec1_part_a_dom` entry (`label_column`, `total_row`, `needs_trim`) — but the prompt's VERTICAL detection and TOTAL-row detection are independently re-derived at prompt-build time from `description_samples.json` via keyword matching (`_TOTAL_ROW_KEYWORDS`) and a live padding check. The semantic layer's authored fields for this table currently agree with the derived ones (both say `total_row='C. Total ( A + B)'`, `needs_trim=False`), but they are two separate code paths computing the same fact — the YAML's version is currently inert.

4. **`RESOLVED TIME CONTEXT` did not fire for "the latest quarter."** `_resolve_relative_time`'s regex vocabulary matches "last quarter" but not "latest quarter" — a one-word phrasing difference silently produced *zero* resolved-date text in the prompt. The model was left to satisfy Rule 6 ("if no period, use MAX(RDATE)") unaided, and in this run it did so correctly — but this is inference from a fallback rule, not from an explicit resolved date the way "last quarter" would have produced.

5. **No sample/distinct values for any numeric or date column.** The sampler (`description_fetcher.py`) only ever samples label-shaped text columns. The model has zero visibility into the actual magnitude, scale, or distribution of `TOTAL_LOAN_ASSETS`, `CODE`, or `RDATE` — only their declared SQL type.

6. **The richer rule block (`_build_full_rules_block`, 26 numbered rules including explicit DOM/OVE, multi-part UNION, and ranking guidance) exists in the codebase but is not used for this model.** It only activates for the `rules` prompt style (`gpt-oss:120b-cloud`, `qwen2.5:7b`), not `ddl` (the active SQLCoder profile). SQLCoder never sees, for example, the explicit DOM/OVE combination rule or the multi-part UNION ALL pattern — this particular table needed neither, but a different table would.

7. **The selector's per-column reasoning (`why` fields) never reached the prompt for the primary query**, because the dominance short-circuit skipped the LLM call entirely — there was no reasoning to discard, but it's worth noting the *capability* (structured per-column justification) exists and simply wasn't exercised here.

8. **For the secondary query, the entire selector call was silently unusable** — a 404 from the configured remote `OLLAMA_URL` for the `SELECTOR_MODEL` name — meaning selection fell back to raw top-1-by-retrieval-score with **no LLM reasoning at all**, in a case that specifically had close retrieval scores (ratio 1.11) where LLM judgment was supposed to matter most. This is an operational/environment finding (model availability on the configured endpoint), not a logic bug — flagged here per the request to note missing context, without proposing a fix.

9. **`needs_trim.json` does not exist**, and the function that reads it (`description_fetcher.load_needs_trim`) has no caller anywhere in the codebase — trim detection is computed independently, live, inside `build_table_ddl`. The file/reader pair is dead infrastructure that duplicates a fact computed elsewhere.

---

## 10. SQL Generation Readiness Score

Scored 1 (poor) – 5 (excellent), grounded in what was actually observed in this trace plus direct inspection of `schema.json`/`semantic_layer.yaml` for the active build.

| Dimension | Score | Basis |
|---|---|---|
| **Schema quality** | 4/5 | Real Oracle-derived types (`NUMBER(20,2)`, `DATE`, correct `CODE NUMBER` typing) reached the prompt correctly; structurally sound `CREATE TABLE` rendering; docked one point for zero declared keys (see Constraint coverage). |
| **Metadata quality** | 4/5 | Every domain column had a real business description sourced from Excel metadata; the two generic-named columns (`code`, `rdate`) had no description beyond their own name; `return_name` is captured but discarded before the prompt. |
| **Retrieval quality** | 5/5 (this query) | Correct table found at rank 1 with a decisive score margin (ratio 3.01); the four-signal RRF fusion worked exactly as designed for this phrasing. (Retrieval quality is inherently query-dependent — this is not a general claim about all queries.) |
| **Label coverage** | 4/5 | 13 real row-label values captured and rendered, correct TOTAL-row detected and correctly *not* over-flagged for TRIM() (this column genuinely isn't padded); coverage exists only for text-shaped label columns, none for numeric/date columns (by design, not a gap for those column types). |
| **Constraint coverage** | **1/5** | **Zero** for this table and, per `data/constraints.json`'s absence, likely zero across the entire active build — no PRIMARY KEY, no FOREIGN KEY reached the prompt anywhere in this trace. The rendering machinery to use them exists and is unused for lack of source data. |
| **Prompt completeness** | 3/5 | Schema, one QA example, and a fixed rule list all present and internally consistent for this query; the time-resolution block silently didn't fire for "latest" phrasing, and the richer 26-rule block is unused for this model tier by design (not a defect, a configuration choice) — but it does mean this specific prompt configuration is less complete than the alternative `rules` style. |

**Overall: 3.5/5** — the trace shows a prompt that is internally consistent and, for this specific query, sufficient to produce correct, validated, executable SQL on the first attempt with zero retries. The two clearest, verified gaps are **constraint coverage (PK/FK not yet populated in this build)** and the **narrow time-phrase vocabulary** in the resolver — both are data/config completeness gaps, not architectural flaws in the pipeline itself.
