"""
Offline regression tests for the accuracy guards.

No database, no LLM, no pytest — every case here is built from the real schema
files plus SQL quoted verbatim out of eval/results/hallucination_log.jsonl, so it
runs anywhere the repo does.

    python -m scripts.test_accuracy_guards

Exits non-zero on the first failing expectation, so it is usable as a pre-commit
or CI gate.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config
from src.description_fetcher import load_samples
from src.retriever import compute_query_embedding, get_relevant_schema
from src.semantic_layer import clear_cache, load_join_graph
from src.sql_generator import (
    _load_table_entries,
    _try_autocorrect_vertical_aggregation,
    build_prompt,
    build_table_ddl,
    validate_sql,
)
from src.selector import _declared_join_selection, select_tables

_failures = []
_passed = 0


def check(label, condition, detail=""):
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failures.append(f"{label}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


def _tables(*names):
    return [{"table": n} for n in names]


def _columns_for(*names):
    from src.sql_generator import _load_all_columns
    return _load_all_columns(list(names))


# ══════════════════════════════════════════════════════════════════════════════
section("1. Invented joins from hallucination_log.jsonl are rejected")

# Verbatim from the log: question "show total loan from cims raq".
fabricated_fk = (
    "SELECT SUM(cims_raq_q_sec1_part_a_dom.total_loan_assets) AS total_loan_assets "
    "FROM cims_raq_q_sec1_part_a_dom "
    "JOIN cims_raq_q_sec7_exp_cr ON cims_raq_q_sec1_part_a_dom.code = cims_raq_q_sec7_exp_cr.code "
    "WHERE cims_raq_q_sec7_exp_cr.cims_raq_q_sec1_part_a_dom_fk = cims_raq_q_sec1_part_a_dom.code"
)
tables = _tables("cims_raq_q_sec1_part_a_dom", "cims_raq_q_sec7_exp_cr")
ok, reason = validate_sql(fabricated_fk, tables, _columns_for(*[t["table"] for t in tables]))
check("fabricated ..._fk join rejected", not ok, f"got ok={ok} reason={reason!r}")

# Verbatim from the log: question "give me risk exposure for domestic".
fabricated_id = (
    "SELECT SUM(cims_raq_q_sec9_sensec_partb.tot_expo_dom) AS total_exposure "
    "FROM cims_raq_q_sec9_sensec_partb "
    "JOIN cims_raq_q_sec2_part_a ON cims_raq_q_sec9_sensec_partb.per_to_total_dom = cims_raq_q_sec2_part_a.id"
)
tables = _tables("cims_raq_q_sec9_sensec_partb", "cims_raq_q_sec2_part_a")
ok, reason = validate_sql(fabricated_id, tables, _columns_for(*[t["table"] for t in tables]))
check("fabricated .id join rejected", not ok, f"got ok={ok} reason={reason!r}")

# Verbatim from the log: truncation coined the table name "cims_raq".
truncated = (
    "SELECT gross_amt_os_dom FROM cims_raq_q_sec2_part_a "
    "UNION ALL SELECT 0 AS gross_amt_os_dom FROM cims_raq"
)
ok, reason = validate_sql(truncated, _tables("cims_raq_q_sec2_part_a"),
                          _columns_for("cims_raq_q_sec2_part_a"))
check("truncated table name 'cims_raq' rejected", not ok, f"got ok={ok} reason={reason!r}")


# ══════════════════════════════════════════════════════════════════════════════
section("1b. Row-label literals are not read as identifiers")

# validate_sql used to strip the quote CHARACTERS and leave the literal TEXT in
# place, so every word inside a row-label literal became a candidate identifier.
# This schema's labels are long English phrases, so correct SQL was rejected —
# and specifically the vertical-table label filtering the prompt instructs the
# model to write, which is the most common query shape here. Both cases below
# were produced by the real model on real questions and were wrongly rejected.
_dom_a = "cims_raq_q_sec1_part_a_dom"
_dom_a_cols = _columns_for(_dom_a)

literal_with_words = (
    "SELECT SUM(CASE WHEN PERIOD_DELINQUENCY = "
    "'ii.a.3 Overdue for 60 to 90 days (SMA -2)' THEN TOTAL_LOAN_ASSETS END) AS sma2 "
    f"FROM {_dom_a.upper()}"
)
ok, reason = validate_sql(literal_with_words, _tables(_dom_a), _dom_a_cols)
check("words inside a label literal are not 'hallucinated columns'", ok,
      f"reason={reason!r}")

literal_starting_with_from = (
    f"SELECT TOTAL_LOAN_ASSETS FROM {_dom_a.upper()} "
    "WHERE PERIOD_DELINQUENCY = 'From Doubtful'"
)
ok, reason = validate_sql(literal_starting_with_from, _tables(_dom_a), _dom_a_cols)
check("a literal beginning 'From ' is not read as a table reference", ok,
      f"reason={reason!r}")

# The masking must not blind the real checks. Each of these has to STILL fail.
ok, _ = validate_sql(
    f"SELECT a.TOTAL_LOAN_ASSETS FROM {_dom_a.upper()} a JOIN SOME_LOOKUP b ON a.CODE = b.CODE",
    _tables(_dom_a), _dom_a_cols)
check("masking still rejects a genuinely invented table", not ok)

ok, _ = validate_sql(
    f"SELECT NOT_A_REAL_COLUMN FROM {_dom_a.upper()} WHERE PERIOD_DELINQUENCY = 'C. Total ( A + B)'",
    _tables(_dom_a), _dom_a_cols)
check("masking still rejects a genuinely invented column", not ok)

ok, _ = validate_sql(
    f"SELECT TOTAL_LOAN_ASSETS FROM {_dom_a.upper()} WHERE a = 'safe' ; DROP TABLE t",
    _tables(_dom_a), _dom_a_cols)
check("masking still rejects a banned keyword outside a literal", not ok)

ok, _ = validate_sql(
    f"SELECT TOTAL_LOAN_ASSETS FROM {_dom_a.upper()} WHERE RDATE = '2024-03-31' "
    "AND PERIOD_DELINQUENCY = 'C. Total ( A + B)'",
    _tables(_dom_a), _dom_a_cols)
check("masking still rejects a bare date literal", not ok)

ok, _ = validate_sql(f"SELECT SUM(TOTAL_LOAN_ASSETS) FROM {_dom_a.upper()}",
                     _tables(_dom_a), _dom_a_cols)
check("masking still rejects a vertical SUM with no label filter", not ok)


# ══════════════════════════════════════════════════════════════════════════════
section("2. The join graph is an allow-list, not a blanket ban")

clear_cache()
graph = load_join_graph()
check("join graph is loaded and non-empty", bool(graph), f"graph={graph}")

declared_pair = ("cims_raq_q_sec1_part_a_dom", "cims_raq_q_sec1_part_b_dom")
check("sibling part_a/part_b pair is declared",
      tuple(sorted(declared_pair)) in graph)

declared_join = (
    "SELECT a.period_delinquency, a.total_loan_assets, b.total_oiba "
    "FROM cims_raq_q_sec1_part_a_dom a "
    "JOIN cims_raq_q_sec1_part_b_dom b ON a.code = b.code AND a.rdate = b.rdate"
)
ok, reason = validate_sql(declared_join, _tables(*declared_pair), _columns_for(*declared_pair))
check("declared sibling join is accepted", ok, f"reason={reason!r}")

# Two real tables, real columns, valid SQL — rejected purely because the
# relationship is not declared. This is the behaviour a prompt bullet could not
# enforce at 7B.
undeclared_but_wellformed = (
    "SELECT a.risk_category, a.gross_amt_os_dom "
    "FROM cims_raq_q_sec2_part_a a "
    "JOIN cims_raq_q_sec10 b ON a.code = b.code AND a.rdate = b.rdate"
)
ok, reason = validate_sql(undeclared_but_wellformed,
                          _tables("cims_raq_q_sec2_part_a", "cims_raq_q_sec10"),
                          _columns_for("cims_raq_q_sec2_part_a", "cims_raq_q_sec10"))
check("undeclared join rejected even with real columns", not ok, f"reason={reason!r}")
check("rejection names the undeclared-join reason",
      not ok and "undeclared join" in reason.lower(), f"reason={reason!r}")


# ══════════════════════════════════════════════════════════════════════════════
section("3. Structured DDL prompt")

prompt = build_prompt("total loan assets for the latest quarter",
                      _tables("cims_raq_q_sec1_part_a_dom"),
                      _columns_for("cims_raq_q_sec1_part_a_dom"),
                      today_date="2026-08-01")

check("prompt renders a CREATE TABLE block", "CREATE TABLE CIMS_RAQ_Q_SEC1_PART_A_DOM (" in prompt)
check("column types are present (not guessed as text)", "NUMBER(20,2)" in prompt)
check("CODE is typed NUMBER, so quoting it is visibly wrong",
      "CODE NUMBER" in prompt, "CODE should carry its real Oracle type")
check("RDATE is typed DATE", "RDATE DATE" in prompt)
check("the total row label is surfaced", "TOTAL row:" in prompt)
check("exactly one table is offered", prompt.count("CREATE TABLE ") == 1,
      f"found {prompt.count('CREATE TABLE ')}")
check("prompt tells the model not to join", "Do not join" in prompt)
check("prompt is ASCII-only (no stray typographic characters)",
      prompt.isascii(), "non-ASCII found")

# Comment placement: the separating comma must precede the `--`, or the comment
# swallows it and the DDL stops being parseable.
ddl_lines = [l for l in prompt.splitlines() if "  -- " in l and l.strip().startswith(("PERIOD", "TERM", "CASH"))]
check("commas are outside trailing comments",
      all(l.split("  -- ")[0].rstrip().endswith(",") or l.split("  -- ")[0].rstrip().endswith(")")
          for l in ddl_lines),
      f"lines={ddl_lines[:2]}")


# ══════════════════════════════════════════════════════════════════════════════
section("4. Whitespace-padded label values use TRIM()")

# The real sampler strips values, so construct the padded case explicitly: this
# is the '     C2. Slipped to NPAs' shape documented in the scoped README.
padded_entry = {
    "table": "padded_demo",
    "description": "demo table",
    "columns": [
        {"name": "description", "type": "VARCHAR2(200)"},
        {"name": "amount", "type": "NUMBER(20,2)"},
        {"name": "code", "type": "NUMBER(20,2)"},
        {"name": "rdate", "type": "DATE"},
    ],
}
padded_ddl = build_table_ddl(
    padded_entry,
    label_values={"description": ["     C2. Slipped to NPAs", "  C. Total ( A + B)"]},
)
check("padded values trigger a TRIM() instruction", "TRIM(DESCRIPTION)" in padded_ddl,
      padded_ddl)
check("padded literals are shown trimmed, so TRIM() comparison matches",
      "'C2. Slipped to NPAs'" in padded_ddl, padded_ddl)
check("total row for a padded column is expressed through TRIM()",
      "TOTAL row: TRIM(DESCRIPTION) = 'C. Total ( A + B)'" in padded_ddl, padded_ddl)

# Unpadded columns must NOT gain a spurious TRIM().
real_entry = _load_table_entries(["cims_raq_q_sec1_part_a_dom"])["cims_raq_q_sec1_part_a_dom"]
real_samples = load_samples().get("cims_raq_q_sec1_part_a_dom", {})
unpadded_ddl = build_table_ddl(real_entry, label_values=real_samples)
check("unpadded column gets no TRIM() noise", "TRIM(" not in unpadded_ddl)

# The deterministic autocorrect must also respect padding.
corrected = _try_autocorrect_vertical_aggregation(
    "SELECT SUM(total_loan_assets) FROM cims_raq_q_sec1_part_a_dom",
    "Table cims_raq_q_sec1_part_a_dom is a vertical table (each row is a named metric via period_delinquency,",
)
check("autocorrect injects the row-label filter", corrected is not None and "period_delinquency =" in (corrected or "").lower(),
      repr(corrected))
check("autocorrect unwraps the SUM()", corrected is not None and "sum(" not in (corrected or "").lower(),
      repr(corrected))


# ══════════════════════════════════════════════════════════════════════════════
section("5. Selector narrows deterministically, no LLM call, fails open to top-1")

shortlist = _tables("cims_raq_q_sec2_part_a", "cims_raq_q_sec10", "cims_raq_q_sec1_part_a_dom")
for i, t in enumerate(shortlist):
    t["score"] = 1.0 - i * 0.05           # deliberately close, so no dominance short-circuit

# A near-duplicate prior question skips everything and picks that table.
strong = [{"table": "cims_raq_q_sec2_part_a", "strong_match": True}] + shortlist[1:]
selected, selection = select_tables("anything", strong)
check("strong QA match short-circuits to 1 table", len(selected) == 1 and selection is None)

# Dominant retrieval score also short-circuits to top-1.
dominant = [{"table": "cims_raq_q_sec2_part_a", "score": 1.0},
            {"table": "cims_raq_q_sec10", "score": 0.2}]
selected, selection = select_tables("anything", dominant)
check("dominant score short-circuits to 1 table", len(selected) == 1 and selection is None)

# Ambiguous shortlist (no dominance, no strong match) with no declared join
# between the top-2 falls open to top-1 — never guesses a second table.
selected, selection = select_tables("anything", shortlist, join_graph=load_join_graph())
check("ambiguous shortlist with no declared top-2 join falls back to top-1",
      len(selected) == 1 and selected[0]["table"] == shortlist[0]["table"] and selection is None,
      f"got {[t['table'] for t in selected]}")

# _declared_join_selection: a table outside a declared pair yields nothing.
outside = _declared_join_selection(shortlist, join_graph=load_join_graph())
check("no declared join among these top-2 tables", outside is None, f"got {outside}")

# A declared join between the top-2 candidates is picked up automatically,
# with the SEMANTIC LAYER's condition (never an invented one — there is no
# model output to invent one from anymore).
declared_shortlist = _tables(*declared_pair)
for i, t in enumerate(declared_shortlist):
    t["score"] = 1.0 - i * 0.05           # close scores, no dominance short-circuit
selected, selection = select_tables("anything", declared_shortlist, join_graph=load_join_graph())
check("declared join between top-2 candidates selects both tables",
      selection is not None and len(selected) == 2,
      f"got {[t['table'] for t in selected]}")
check("declared join's hint carries the declared condition",
      selection is not None and selection.get("join_hint")
      and "CODE" in selection["join_hint"] and "RDATE" in selection["join_hint"],
      f"got {selection}")


# ══════════════════════════════════════════════════════════════════════════════
section("6. Column pruning from the selection reaches the prompt")

pruned = build_prompt(
    "total loan assets",
    _tables("cims_raq_q_sec1_part_a_dom"),
    _columns_for("cims_raq_q_sec1_part_a_dom"),
    today_date="2026-08-01",
    selection={"tables": [{"table": "cims_raq_q_sec1_part_a_dom",
                           "columns": [{"name": "total_loan_assets", "why": "the metric"}]}],
               "join_hint": None},
)
check("selected column is present", "TOTAL_LOAN_ASSETS" in pruned)
check("unselected column is pruned", "BILLS_PURCHASE_DISCOUNT" not in pruned, pruned)
check("key columns survive pruning", "CODE " in pruned and "RDATE " in pruned)
check("row-label column survives pruning", "PERIOD_DELINQUENCY" in pruned)


# ══════════════════════════════════════════════════════════════════════════════
section("7. An explicit section reference wins over embedding similarity")

# Regression set from 11 real tested questions. Five of these previously landed
# on a different, unrelated table whose COLUMNS resembled the wording more than
# the named section did (e.g. interest-rate questions naming Section 12 Misc T3
# were answered from SEC10). The last four name no section and must be
# unaffected — they were already correct and must not regress.
_SECTION_CASES = [
    ("What is the total funded exposure from RAQ Section 10 for the latest reporting date?",
     "cims_raq_q_sec10"),
    ("What is the total exposure from RAQ Section 10 for the latest reporting date?",
     "cims_raq_q_sec10"),
    ("Show the gross advances from Section 12 Misc T4 for the latest reporting date.",
     "cims_raq_q_sec_12_misc_t4"),
    ("Show the gross NPA from Section 12 Misc T4 for the latest reporting date.",
     "cims_raq_q_sec_12_misc_t4"),
    ("What is the weighted average interest rate in Section 12 Misc T3 for the latest reporting date?",
     "cims_raq_q_sec_12_misc_t3"),
    ("Show the minimum interest rate charged in Section 12 Misc T3.",
     "cims_raq_q_sec_12_misc_t3"),
    ("Show the maximum interest rate charged in Section 12 Misc T3.",
     "cims_raq_q_sec_12_misc_t3"),
    # No section named — pure embedding retrieval, must stay correct.
    ("What is the total loan assets for the latest reporting date?",
     "cims_raq_q_sec1_part_a_dom"),
    ("Show total OIBA by delinquency bucket.", "cims_raq_q_sec1_part_b_dom"),
    ("Show inter-bank assets by period of delinquency.", "cims_raq_q_sec1_part_b_dom"),
    ("Calculate gross NPA percentage of gross advances.", "cims_raq_q_sec_12_misc_t4"),
]

for _question, _want in _SECTION_CASES:
    _tables_out = get_relevant_schema(_question, shortlist_k=config.SHORTLIST_K).tables
    _got = _tables_out[0]["table"] if _tables_out else None
    check(f"rank-0 table for: {_question[:44]}",
          (_got or "").lower() == _want.lower(),
          f"got {_got!r}, want {_want!r}")

# A section number that exists in the question but not in this build must be
# ignored rather than emptying the shortlist.
_unknown = get_relevant_schema("show me section 99 data", shortlist_k=config.SHORTLIST_K).tables
check("unknown section number falls back to normal retrieval", len(_unknown) > 0)

# "Section 12" alone cannot distinguish misc / t2 / t3 / t4 — retrieval should
# stay INSIDE section 12 rather than guessing one, or leaking other sections in.
_ambig = get_relevant_schema("show me section 12 data", shortlist_k=config.SHORTLIST_K).tables
check("ambiguous section scopes to that section only",
      len(_ambig) > 0 and all("sec_12" in t["table"].lower() for t in _ambig),
      f"got {[t['table'] for t in _ambig]}")


section("8. XBRL business-semantics layer (src/concept_map, src/business_semantics)")
# Guards the facts the taxonomy contributes that nothing else in the repo knows,
# and the two readings of it that would produce confidently wrong SQL — see
# _mark_row_code_usability in build_concept_map.py.

import src.business_semantics as _bs
from src.concept_map import (
    check_stock_aggregation,
    formula_for,
    load_concept_map,
    metrics_for_table,
    stock_columns,
    unit_multiplier_for_table,
)

_cm = load_concept_map()
check("concept_map.json loads", bool(_cm.get("metrics")),
      f"{len(_cm.get('metrics') or [])} metrics")

if _cm.get("metrics"):
    # ── the unit fact ────────────────────────────────────────────────────────
    check("monetary table reports a lakh multiplier",
          unit_multiplier_for_table("CIMS_RAQ_Q_SEC8_INFRA_BRKUP") == 100000,
          f"got {unit_multiplier_for_table('CIMS_RAQ_Q_SEC8_INFRA_BRKUP')}")
    check("unknown table makes no unit claim",
          unit_multiplier_for_table("NO_SUCH_TABLE") is None)

    # ── row_code: a band start must NOT be offered as a row selector ─────────
    # Every concept on the wide infrastructure table carries filter_value 1000,
    # and CODE 1000 is the row '1. Transport and adjoining Infrastructure'.
    # Emitting CODE = 1000 for 'Actual recoveries' would pin every answer there.
    _infra = metrics_for_table("CIMS_RAQ_Q_SEC8_INFRA_BRKUP")
    check("band-start code is not treated as a row selector",
          _infra and not any(m["row_code_selects_metric"] for m in _infra),
          f"{sum(bool(m['row_code_selects_metric']) for m in _infra)}/{len(_infra)} selectable")

    # A vertical key-value table is the opposite case: CODE genuinely names the
    # metric, so the selector MUST be offered.
    _gen = [m for m in metrics_for_table("CIMS_RAQ_Q_GEN_INFO") if m["row_code"] is not None]
    check("key-value code IS treated as a row selector",
          _gen and all(m["row_code_selects_metric"] for m in _gen),
          f"{sum(bool(m['row_code_selects_metric']) for m in _gen)}/{len(_gen)}")

    # 'WHERE CODE = BankCode' is either a syntax error or a zero-row match.
    _nonnum = [m["row_code"] for m in _cm["metrics"]
               if m["row_code"] is not None and not isinstance(m["row_code"], int)]
    check("every shipped row_code is numeric", not _nonnum, str(_nonnum[:5]))

    # ── derivations must never collapse to an identity ───────────────────────
    _bad = []
    for _t in _cm["table_profiles"]:
        for _m in metrics_for_table(_t):
            _f = formula_for(_m, _t)
            if _f and len(set(_f["columns"])) < len(_f["columns"]):
                _bad.append((_t, _m["label"]))
    check("no derivation repeats a column (would be an identity)", not _bad, str(_bad[:3]))

    # ── stock/flow aggregation warning ──────────────────────────────────────
    _stock_tbl = next((t for t in _cm["table_profiles"] if stock_columns(t)), None)
    check("at least one stock column is known", _stock_tbl is not None)
    if _stock_tbl:
        _stock_col = sorted(stock_columns(_stock_tbl))[0]
        _targ = [{"table": _stock_tbl}]
        check("SUM of a stock without an RDATE filter warns",
              check_stock_aggregation(f"SELECT SUM({_stock_col}) FROM {_stock_tbl}", _targ),
              f"{_stock_tbl}.{_stock_col}")
        # Pinned to one period, the same SUM aggregates across dimension rows
        # within a single date, which is legitimate.
        check("SUM of a stock pinned to one RDATE does not warn",
              not check_stock_aggregation(
                  f"SELECT SUM({_stock_col}) FROM {_stock_tbl} "
                  f"WHERE RDATE = (SELECT MAX(RDATE) FROM {_stock_tbl})", _targ))
        check("plain SELECT of a stock does not warn",
              not check_stock_aggregation(f"SELECT {_stock_col} FROM {_stock_tbl}", _targ))
        # Regression guard for a live-model bug: SQLCoder produced
        # `RDATE <= (SELECT MAX(RDATE) FROM t)` for "across all reporting
        # quarters" — spans every period up to the max, the OPPOSITE of
        # pinning to one — and the previous check's bare "max(rdate)"
        # substring match let it through with no warning.
        check("SUM of a stock spanning ALL periods (RDATE <=) still warns",
              check_stock_aggregation(
                  f"SELECT SUM({_stock_col}) FROM {_stock_tbl} "
                  f"WHERE RDATE <= (SELECT MAX(RDATE) FROM {_stock_tbl})", _targ))

    # ── the level switch must actually gate output ───────────────────────────
    _saved = config.BUSINESS_SEMANTICS_LEVEL
    try:
        config.BUSINESS_SEMANTICS_LEVEL = "off"
        check("level=off renders nothing",
              _bs.build_block("recoveries", "CIMS_RAQ_Q_SEC8_INFRA_BRKUP", {}) == ""
              and _bs.build_rules("CIMS_RAQ_Q_SEC8_INFRA_BRKUP") == [])

        config.BUSINESS_SEMANTICS_LEVEL = "units"
        _u = _bs.build_block("recoveries", "CIMS_RAQ_Q_SEC8_INFRA_BRKUP", {})
        check("level=units states the unit", "LAKH" in _u, _u[:80])
        check("level=units withholds metric cards", "Business metrics" not in _u)

        config.BUSINESS_SEMANTICS_LEVEL = "derivation"
        _full = _bs.build_block(
            "actual recoveries", "CIMS_RAQ_Q_SEC8_INFRA_BRKUP",
            {"industry_name": ["2. Energy", "2.1 Electricity Generation"]})
        check("full level adds metric cards", "Business metrics" in _full)
        check("full level adds dimensions", "Dimensions available" in _full)
        # Each stored literal must appear once; duplicates are pure prompt waste.
        _dim = [ln for ln in _full.splitlines() if "INDUSTRY_NAME =" in ln]
        check("dimension literals are not duplicated",
              len(_dim) == len(set(_dim)), str(_dim))

        _compact = _bs.build_block(
            "actual recoveries", "CIMS_RAQ_Q_SEC8_INFRA_BRKUP",
            {"industry_name": ["2. Energy"]}, compact=True)
        check("compact caps at aggregation",
              "Dimensions available" not in _compact and "LAKH" in _compact)

        # A table the taxonomy says nothing about gets no block at all, rather
        # than an empty-but-present section header in the prompt.
        check("unmapped table renders no block",
              _bs.build_block("anything", "NO_SUCH_TABLE", {}) == "")
    finally:
        config.BUSINESS_SEMANTICS_LEVEL = _saved


section("9. Hallucinated-literal check (src/literal_validator.py)")
# Confirmed live-model bug: SQLCoder generated
# `WHERE INDUSTRY_NAME = 'Infrastructure Breakdown'`, which is not one of the
# real stored INDUSTRY_NAME values on CIMS_RAQ_Q_SEC8_INFRA_BRKUP. The SQL
# validated and executed cleanly and silently returned 0 rows.

from src.literal_validator import check_literal_validity

_lit_tbl = "cims_raq_q_sec8_infra_brkup"
_lit_samples = load_samples().get(_lit_tbl, {})
check("description_samples.json has industry_name values for the infra table",
      bool(_lit_samples.get("industry_name")))

if _lit_samples.get("industry_name"):
    _real_value = _lit_samples["industry_name"][0]
    _lit_targ = [{"table": _lit_tbl}]

    check("a real stored value does not warn",
          not check_literal_validity(
              f"SELECT SUM(ACTUAL_RECOVERIES) FROM {_lit_tbl} "
              f"WHERE INDUSTRY_NAME = '{_real_value}'", _lit_targ),
          _real_value)

    # The exact hallucinated literal reproduced live this session.
    check("the reproduced hallucinated literal warns",
          check_literal_validity(
              f"SELECT SUM(ACTUAL_RECOVERIES) FROM {_lit_tbl} "
              f"WHERE INDUSTRY_NAME = 'Infrastructure Breakdown'", _lit_targ))

    # A value needing TRIM() normalization still matches if the trimmed form
    # is a real stored value — no false positive from surrounding whitespace.
    check("a padded value wrapped in TRIM() does not warn",
          not check_literal_validity(
              f"SELECT SUM(ACTUAL_RECOVERIES) FROM {_lit_tbl} "
              f"WHERE TRIM(INDUSTRY_NAME) = '  {_real_value}  '", _lit_targ),
          repr(_real_value))

    check("an untracked column is skipped, not flagged",
          not check_literal_validity(
              f"SELECT * FROM {_lit_tbl} WHERE SOME_UNTRACKED_COL = 'anything'",
              _lit_targ))

    check("a table with no tracked samples at all is skipped, not flagged",
          not check_literal_validity(
              "SELECT * FROM NO_SUCH_TABLE WHERE INDUSTRY_NAME = 'anything'",
              [{"table": "NO_SUCH_TABLE"}]))


section("10. QA-bonus prune-floor fix (src/retriever.py)")
# Bug: RELATIVE_FLOOR used to be computed off the POST-bonus top score, so a
# confident QA match (bonus up to ~1.25, vs typical fused scores of 0.05-0.4)
# inflated the reference every OTHER table's prune floor was measured
# against, silently dropping genuinely relevant tables before the selector
# ever saw them. Fixed by snapshotting the floor reference pre-bonus.
#
# A question near-identical to a qa_pairs.json entry triggers the QA bonus.
# Widen shortlist_k so pruning, not top-k truncation, is what's being
# observed — the fix can only ADMIT tables the old floor excluded, never
# exclude ones it admitted, so this comparison is structurally one-directional.
_qa_bonus_q = "What is the total SLR investment for RAQ?"
_qa_vec = compute_query_embedding(_qa_bonus_q)

_saved_fix_flag = config.QA_BONUS_FLOOR_FIX
try:
    config.QA_BONUS_FLOOR_FIX = 0
    _old_tables = {t["table"] for t in
                   get_relevant_schema(_qa_bonus_q, query_vec=_qa_vec, shortlist_k=50).tables}

    config.QA_BONUS_FLOOR_FIX = 1
    _new_tables = {t["table"] for t in
                   get_relevant_schema(_qa_bonus_q, query_vec=_qa_vec, shortlist_k=50).tables}

    check("fixed floor never excludes a table the old floor admitted",
          _old_tables <= _new_tables,
          f"lost: {_old_tables - _new_tables}")
    check("fixed floor admits at least as many tables as the old floor",
          len(_new_tables) >= len(_old_tables),
          f"old={len(_old_tables)} new={len(_new_tables)}")
finally:
    config.QA_BONUS_FLOOR_FIX = _saved_fix_flag


section("11. Correction-retry loop stops on a repeated identical mistake (src/sql_generator.py)")
# Bug: without failed-attempt memory, a retry round rebuilds its prompt purely
# from the CURRENT raw/reason, so if the model reproduces the exact same wrong
# SQL, every remaining round repeats it too — pure wasted latency (75-135s+
# per Ollama round observed against this backend) with zero chance of
# eventual success. Mocks the Ollama HTTP call to always return the same
# hallucinated-table SQL, so the loop is guaranteed to never validate; the fix
# should stop after the FIRST retry reproduces attempt 0's SQL verbatim,
# rather than burning all MAX_CORRECTION_RETRIES rounds on a guaranteed repeat.
import unittest.mock as _mock
from src.sql_generator import generate_sql as _generate_sql, MAX_CORRECTION_RETRIES as _MAX_RETRIES

_HALLUCINATED_SQL = "SELECT * FROM TOTALLY_MADE_UP_TABLE_XYZ"


class _FakeResponse:
    def raise_for_status(self):
        pass

    def iter_lines(self):
        import json as _json
        yield _json.dumps({"response": _HALLUCINATED_SQL, "done": True}).encode()


_real_tables = [{"table": "CIMS_RAQ_Q_SEC10"}]
_real_columns = []

with _mock.patch("src.sql_generator.requests.post", return_value=_FakeResponse()) as _post_mock, \
     _mock.patch("src.executor.dry_run_sql", return_value=(True, None)):
    _result = _generate_sql("What is the total for section 10?", _real_tables, _real_columns)

# 1 first_attempt call + however many retries actually ran before the fix
# would stop early on the repeat vs. always running the full budget.
_calls_made = _post_mock.call_count
check("correction loop stops after the first repeated-SQL round, not the full retry budget",
      _calls_made < 1 + _MAX_RETRIES,
      f"calls made={_calls_made} (1 first_attempt + up to {_MAX_RETRIES} retries)")
check("the final SQL returned is still the hallucinated one (no silent corruption)",
      _result.get("sql", "").strip() == _HALLUCINATED_SQL)
check("a warning is present explaining the invalid SQL",
      any("invalid" in w.lower() or "hallucinat" in w.lower() for w in _result.get("warnings", [])))


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 60}")
if _failures:
    print(f"{len(_failures)} FAILED, {_passed} passed\n")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"All {_passed} checks passed  (embedding dir: {config.EMBEDDING_DIR})")
