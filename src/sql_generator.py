import re
import json
import calendar
import requests
from datetime import date, timedelta
import src.config as config

BANNED_KEYWORDS = ["delete", "update", "drop", "insert", "truncate", "alter", "create", "exec"]
MAX_LABELS_MINIMAL = 8


def _resolve_relative_time(query: str, today: date) -> str | None:
    """
    Detect relative time expressions in the user query and resolve them to
    concrete calendar date ranges.  The returned block is injected into the
    SQL prompt so the LLM never has to guess what 'last quarter' means.
    """
    q = query.lower()
    lines = []

    # ── helpers ──────────────────────────────────────────────────────────────
    def fmt(d): return d.strftime("%Y-%m-%d")
    def month_end(y, m): return date(y, m, calendar.monthrange(y, m)[1])

    # ── This / last WEEK ─────────────────────────────────────────────────────
    if re.search(r'\b(this|current)\s+week\b', q):
        mon = today - timedelta(days=today.weekday())
        sun = mon + timedelta(days=6)
        lines.append(f"'this week'  = {fmt(mon)} to {fmt(sun)}")

    if re.search(r'\b(last|previous)\s+week\b', q):
        mon = today - timedelta(days=today.weekday() + 7)
        sun = mon + timedelta(days=6)
        lines.append(f"'last week'  = {fmt(mon)} to {fmt(sun)}")

    # ── This / last MONTH ────────────────────────────────────────────────────
    if re.search(r'\b(this|current)\s+month\b', q):
        start = today.replace(day=1)
        end = month_end(today.year, today.month)
        lines.append(f"'this month' = {today.strftime('%B %Y')}  ({fmt(start)} to {fmt(end)})")

    if re.search(r'\b(last|previous)\s+month\b', q):
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
        lines.append(f"'last month' = {end.strftime('%B %Y')}  ({fmt(start)} to {fmt(end)})")

    # ── helper: subtract N whole months from a date, returning (year, month) ───
    def _months_back(y, m, n):
        idx = (y * 12 + (m - 1)) - n
        return idx // 12, idx % 12 + 1

    _WORD_NUM = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    }

    def _extract_n(token: str) -> int:
        return int(token) if token.isdigit() else _WORD_NUM.get(token.lower(), 0)

    _N_WORD_RE = r'(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)'

    # ── Last/previous/past N MONTHS ──────────────────────────────────────────
    # Matches: "last 3 months", "past 6 months", "previous two months", "trailing 4 months"
    m_months = re.search(rf'\b(?:last|previous|past|trailing)\s+{_N_WORD_RE}\s+months?\b', q)
    if m_months:
        n = _extract_n(m_months.group(1))
        if n > 0:
            # The N complete months immediately preceding the current (in-progress) month.
            end_y, end_m = _months_back(today.year, today.month, 1)
            end = month_end(end_y, end_m)
            start_y, start_m = _months_back(today.year, today.month, n)
            start = date(start_y, start_m, 1)
            lines.append(
                f"'last {n} months' = {start.strftime('%B %Y')} to {end.strftime('%B %Y')}  "
                f"({fmt(start)} to {fmt(end)})"
            )

    # ── Last/previous/past N YEARS ───────────────────────────────────────────
    m_years = re.search(rf'\b(?:last|previous|past|trailing)\s+{_N_WORD_RE}\s+years?\b', q)
    if m_years:
        n = _extract_n(m_years.group(1))
        if n > 0:
            end_y = today.year - 1
            start_y = today.year - n
            lines.append(
                f"'last {n} years' = {start_y} to {end_y}  "
                f"(01-JAN-{start_y} to 31-DEC-{end_y})"
            )

    # ── This / last YEAR (calendar) ──────────────────────────────────────────
    if re.search(r'\b(this|current)\s+year\b', q):
        lines.append(f"'this year'  = {today.year}  (01-JAN-{today.year} to 31-DEC-{today.year})")

    if re.search(r'\b(last|previous)\s+year\b', q):
        y = today.year - 1
        lines.append(f"'last year'  = {y}  (01-JAN-{y} to 31-DEC-{y})")

    # ── Calendar QUARTER (Jan-Mar / Apr-Jun / Jul-Sep / Oct-Dec) ─────────────
    _CQ_START = {1: (1, 1),  2: (4, 1),  3: (7, 1),  4: (10, 1)}
    _CQ_END   = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    cur_cq = (today.month - 1) // 3 + 1

    if re.search(r'\b(this|current)\s+quarter\b', q):
        s = date(today.year, *_CQ_START[cur_cq])
        e = date(today.year, *_CQ_END[cur_cq])
        lines.append(f"'this quarter' = Q{cur_cq} {today.year} (calendar)  ({fmt(s)} to {fmt(e)})")

    if re.search(r'\b(last|previous)\s+quarter\b', q):
        prev_cq = cur_cq - 1 if cur_cq > 1 else 4
        prev_cq_year = today.year if cur_cq > 1 else today.year - 1
        s = date(prev_cq_year, *_CQ_START[prev_cq])
        e = date(prev_cq_year, *_CQ_END[prev_cq])
        lines.append(f"'last quarter' = Q{prev_cq} {prev_cq_year} (calendar)  ({fmt(s)} to {fmt(e)})")

    # ── Last/previous/past N QUARTERS (calendar) ─────────────────────────────
    # Matches: "last 3 quarters", "past 4 quarters", "previous two quarters"
    # Range = the N most recently COMPLETED calendar quarters (current quarter excluded).
    m_quarters = re.search(rf'\b(?:last|previous|past|trailing)\s+{_N_WORD_RE}\s+quarters?\b', q)
    if m_quarters:
        n = _extract_n(m_quarters.group(1))
        if n > 0:
            end_cq, end_cq_year = cur_cq - 1, today.year
            if end_cq == 0:
                end_cq, end_cq_year = 4, today.year - 1
            # step back (n - 1) more quarters from the end quarter to get the start quarter
            start_idx = (end_cq_year * 4 + (end_cq - 1)) - (n - 1)
            start_cq_year, start_cq = start_idx // 4, start_idx % 4 + 1
            s = date(start_cq_year, *_CQ_START[start_cq])
            e = date(end_cq_year, *_CQ_END[end_cq])
            unit = "quarter" if n == 1 else "quarters"
            lines.append(
                f"'last {n} {unit}' = Q{start_cq} {start_cq_year} to Q{end_cq} {end_cq_year} (calendar)  "
                f"({fmt(s)} to {fmt(e)})"
            )

    # ── Financial Year quarters (India Apr–Mar, Q1=Apr-Jun … Q4=Jan-Mar) ─────
    fy_sy = today.year if today.month >= 4 else today.year - 1  # FY start year
    _FYQ = {
        1: {"months": {4, 5, 6},    "start": (4, 1),  "end": (6, 30),  "offset": 0},
        2: {"months": {7, 8, 9},    "start": (7, 1),  "end": (9, 30),  "offset": 0},
        3: {"months": {10, 11, 12}, "start": (10, 1), "end": (12, 31), "offset": 0},
        4: {"months": {1, 2, 3},    "start": (1, 1),  "end": (3, 31),  "offset": 1},
    }
    cur_fyq = next(fq for fq, v in _FYQ.items() if today.month in v["months"])

    if re.search(r'\b(this|current)\s+(fy|financial|fiscal)\s*(quarter|q)\b', q):
        off = _FYQ[cur_fyq]["offset"]
        s = date(fy_sy + off, *_FYQ[cur_fyq]["start"])
        e = date(fy_sy + off, *_FYQ[cur_fyq]["end"])
        lines.append(f"'this FY quarter' = Q{cur_fyq} FY{fy_sy+1}  ({fmt(s)} to {fmt(e)})")

    if re.search(r'\b(last|previous)\s+(fy|financial|fiscal)\s*(quarter|q)\b', q):
        prev_fyq = cur_fyq - 1 if cur_fyq > 1 else 4
        prev_fy_sy = fy_sy if cur_fyq > 1 else fy_sy - 1
        off = _FYQ[prev_fyq]["offset"]
        s = date(prev_fy_sy + off, *_FYQ[prev_fyq]["start"])
        e = date(prev_fy_sy + off, *_FYQ[prev_fyq]["end"])
        lines.append(f"'last FY quarter' = Q{prev_fyq} FY{prev_fy_sy+1}  ({fmt(s)} to {fmt(e)})")

    # ── Last/previous/past N FY QUARTERS ─────────────────────────────────────
    # Matches: "last 3 fy quarters", "past 2 fiscal quarters"
    # FY quarters run consecutively Q1→Q4 each starting April; index them on a
    # continuous timeline so stepping back N just means subtracting N from the index.
    m_fyq = re.search(rf'\b(?:last|previous|past|trailing)\s+{_N_WORD_RE}\s+(?:fy|financial|fiscal)\s*(?:quarter|q)s?\b', q)
    if m_fyq:
        n = _extract_n(m_fyq.group(1))
        if n > 0:
            # continuous FY-quarter index: FY_start_year*4 + (fyq-1), counting from current FY quarter
            cur_idx = fy_sy * 4 + (cur_fyq - 1)
            end_idx = cur_idx - 1               # most recently completed FY quarter
            start_idx = end_idx - (n - 1)
            end_fy_sy, end_fyq = end_idx // 4, end_idx % 4 + 1
            start_fy_sy, start_fyq = start_idx // 4, start_idx % 4 + 1
            s = date(start_fy_sy + _FYQ[start_fyq]["offset"], *_FYQ[start_fyq]["start"])
            e = date(end_fy_sy + _FYQ[end_fyq]["offset"], *_FYQ[end_fyq]["end"])
            lines.append(
                f"'last {n} FY quarters' = Q{start_fyq} FY{start_fy_sy+1} to Q{end_fyq} FY{end_fy_sy+1}  "
                f"({fmt(s)} to {fmt(e)})"
            )

    # ── This / last FINANCIAL YEAR ────────────────────────────────────────────
    if re.search(r'\b(this|current)\s+(financial year|fiscal year|fy)\b(?!\s*(?:quarter|q)\b)', q):
        lines.append(
            f"'this financial year' = FY{fy_sy+1}  "
            f"(01-APR-{fy_sy} to 31-MAR-{fy_sy+1})"
        )

    if re.search(r'\b(last|previous)\s+(financial year|fiscal year|fy)\b(?!\s*(?:quarter|q)\b)', q):
        lines.append(
            f"'last financial year' = FY{fy_sy}  "
            f"(01-APR-{fy_sy-1} to 31-MAR-{fy_sy})"
        )

    # ── Generic "last N periods" / "last N reporting periods" ───────────────
    # A "period" here is whatever granularity RDATE reports at for that table
    # (monthly, quarterly, etc.) — it cannot be resolved to a fixed calendar
    # range up front, so instead we tell the LLM exactly how to fetch the last
    # N distinct RDATE rows. Only fires if no more specific unit (month/
    # quarter/year/FY quarter) was already matched above, so an explicit
    # "last 3 quarters" still gets the precise calendar-quarter resolution.
    m_periods = re.search(rf'\b(?:last|previous|past|trailing)\s+{_N_WORD_RE}\s+(?:periods?|reporting\s+periods?)\b', q)
    if m_periods and not any(("quarter" in l or "month" in l or "year" in l) for l in lines):
        n = _extract_n(m_periods.group(1))
        if n > 0:
            lines.append(
                f"'last {n} periods' = the {n} most recent distinct RDATE values for the table "
                f"(NOT a fixed calendar range). Resolve with:\n"
                f"    WHERE RDATE IN (\n"
                f"      SELECT DISTINCT RDATE FROM <same_table>\n"
                f"      ORDER BY RDATE DESC FETCH FIRST {n} ROWS ONLY\n"
                f"    )"
            )

    if not lines:
        return None

    block = (
        "════════════════════════════════════════════════\n"
        "RESOLVED TIME CONTEXT\n"
        "(these are the EXACT date ranges for the relative terms the user wrote)\n"
        "════════════════════════════════════════════════\n"
    )
    block += "\n".join(f"  {l}" for l in lines)
    block += "\n"
    return block

# SQL keywords and functions to skip during column validation
_SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "in", "is", "null",
    "as", "on", "join", "inner", "outer", "left", "right", "full", "cross",
    "group", "by", "order", "having", "distinct", "between", "like", "case",
    "when", "then", "else", "end", "union", "all", "exists", "limit", "offset",
    "count", "sum", "avg", "min", "max", "coalesce", "nvl", "trim", "upper",
    "lower", "to_date", "to_char", "rownum", "dual", "with", "asc", "desc",
    # window function keywords
    "over", "partition", "rows", "range", "unbounded", "preceding",
    "following", "current", "row", "window", "rank", "dense_rank",
    "row_number", "ntile", "lag", "lead", "first_value", "last_value",
}


def _load_all_columns(table_names, schema_path="output/schema.json"):
    """Return all columns for the given table names loaded from schema.json."""
    try:
        with open(schema_path) as f:
            schema = json.load(f)
    except FileNotFoundError:
        return []
    normalized_table_names = {name.lower() for name in table_names}
    result = []
    for entry in schema:
        table_name = entry.get("table") or entry.get("table_name")
        if not table_name or table_name.lower() not in normalized_table_names:
            continue

        for col in entry.get("columns") or []:
            column_name = col.get("name") or col.get("column_name")
            if not column_name:
                continue
            result.append({"table": table_name, "column": column_name})
    return result


def _get_model_profile(model_name):
    default_profile = {
        "prompt_style": "rules",
        "dialect_hint": "Oracle",
        "supports_full_ruleset": True,
        "temperature": None,
        "num_predict": None,
    }
    profile = config.MODEL_PROFILES.get(model_name, {})
    return {**default_profile, **profile}


def _validation_failure_category(reason: str) -> str:
    reason = reason.lower()
    if "dangerous keyword" in reason:
        return "banned keyword"
    if "hallucinated tables" in reason:
        return "hallucinated table"
    if "hallucinated columns" in reason:
        return "hallucinated column"
    if "does not reference any matched table" in reason:
        return "schema grounding"
    return "unknown validation"


def _build_full_rules_block(dialect_hint: str) -> str:
    return f"""You are an expert {dialect_hint} SQL generator.
Use {dialect_hint} syntax, including Oracle-specific constructs such as FETCH FIRST N ROWS ONLY, ADD_MONTHS, EXTRACT(YEAR FROM ...), and TO_DATE(...,'YYYY-MM-DD').

════════════════════════════════════════════════
ABSOLUTE RULES (never break these)
════════════════════════════════════════════════
1. Return ONLY a raw SQL SELECT query — no explanation, no markdown, no code fences, no semicolon.
2. Use ONLY table names and column names listed in the SCHEMA CONTEXT below. Never invent names.
3. Never use bind variables or placeholders (:val, ?, %s). Embed all values as literals.
4. Never touch backup tables (_bkup, _bk, _bckup, _backup suffixes). Use only the main tables.

════════════════════════════════════════════════
VERTICAL FORMAT TABLES — CRITICAL RULES
════════════════════════════════════════════════
Any table tagged "STORAGE FORMAT: VERTICAL" below stores data as named rows.
Each row is one pre-computed metric (e.g. "Substandard", "Total", "Grand Total").
The database already has aggregated/total rows — DO NOT re-aggregate them.

RULE V1 — NEVER aggregate vertically:
  WRONG:  SELECT SUM(TOTAL_LOAN_ASSETS) FROM CIMS_RAQ_Q_SEC1_PART_A_DOM
  RIGHT:  SELECT TOTAL_LOAN_ASSETS FROM CIMS_RAQ_Q_SEC1_PART_A_DOM
          WHERE PERIOD_DELINQUENCY = 'C. Total ( A + B)'

RULE V2 — To get the TOTAL / OVERALL value:
  Use WHERE <label_col> = '<*** TOTAL ROW ***>' shown for each table below.
  Never skip the WHERE clause on a vertical table.

RULE V3 — To get a SPECIFIC metric:
  Use WHERE <label_col> = '<exact value from Known row labels list>'
  Match the label value CHARACTER FOR CHARACTER — case, spaces, dots, brackets all matter.

RULE V4 — LIKE fallback (use ONLY when no exact match is available):
  If the value you need is NOT listed in the Known row labels, use:
    WHERE <label_col> LIKE '%keyword%'
  NEVER invent or guess an exact string — use LIKE if uncertain.

RULE V5 — You MAY use SUM/AVG only when:
  (a) The table is NOT tagged VERTICAL, OR
  (b) You are aggregating across CODE or RDATE partitions on a non-label column
      (e.g. total across all bank codes: SUM(col) GROUP BY RDATE on a non-vertical table).

════════════════════════════════════════════════
DOM / OVE COLUMN RULES  (Domestic + Overseas)
════════════════════════════════════════════════
Many tables store the same metric in two separate columns:
  <col>_DOM  → domestic value
  <col>_OVE  → overseas value

RULE D1 — When the user asks for "total", "combined", "overall", or does NOT specify
  domestic/overseas, add both columns:
    SELECT (EXPOSURE_DOM + EXPOSURE_OVE) AS TOTAL_EXPOSURE ...

RULE D2 — When the user asks for "domestic only" → use <col>_DOM alone.
  When the user asks for "overseas only"  → use <col>_OVE alone.

RULE D3 — When comparing domestic vs overseas, select both columns separately:
    SELECT CODE, RDATE, EXPOSURE_DOM, EXPOSURE_OVE FROM ...

RULE D4 — Do NOT invent a column named TOTAL_<x>; compute it inline as (<col>_DOM + <col>_OVE).

════════════════════════════════════════════════
MULTI-PART / MULTI-SECTION RULES
════════════════════════════════════════════════
Some sections split their data across multiple sibling tables
(e.g. SEC1_PART_A_DOM and SEC1_PART_B_DOM, or SEC9_SENSEC_PARTA and SEC9_SENSEC_PARTB).
Part A typically covers one loan/asset category; Part B covers another.

RULE M1 — If the user asks for a COMBINED / OVERALL total that spans multiple parts of the
  SAME section, use a UNION ALL subquery and then SUM the result:

  EXAMPLE — "total gross NPA across Part A and Part B of SEC1":
    SELECT SUM(val) AS TOTAL_GROSS_NPA
    FROM (
      SELECT GROSS_NPA_END AS val FROM CIMS_RAQ_Q_SEC1_PART_A_DOM
        WHERE PERIOD_DELINQUENCY = '<total row label>'
      UNION ALL
      SELECT GROSS_NPA_END AS val FROM CIMS_RAQ_Q_SEC1_PART_B_DOM
        WHERE PERIOD_DELINQUENCY = '<total row label>'
    )

RULE M2 — Use UNION ALL (not UNION) to preserve all rows including duplicates.

RULE M3 — When the user only mentions one part explicitly (e.g. "Part A"), query ONLY that
  part — do NOT union with other parts.

RULE M4 — When unioning vertical tables, apply the SAME label WHERE filter in each branch.
  Never union without label filters on vertical tables.

RULE M5 — When the same metric exists across multiple sections (e.g. NPA in SEC1 and SEC3),
  use UNION ALL with a literal section tag column so the user can distinguish results:
    SELECT 'SEC1' AS section, NPA_AMT FROM CIMS_RAQ_Q_SEC1_PART_A_DOM WHERE ...
    UNION ALL
    SELECT 'SEC3' AS section, NPA_AMT FROM CIMS_RAQ_Q_SEC3_DETAILS WHERE ...

════════════════════════════════════════════════
JOIN RULES
════════════════════════════════════════════════
RULE J1 — Use JOINs ONLY when the user question requires data from multiple tables.
RULE J2 — Always join on CODE and RDATE together to avoid cross-joining reporting periods.
RULE J3 — For optional/possibly-missing rows use LEFT JOIN, not INNER JOIN.
RULE J4 — Never JOIN a main table with its own backup table.
RULE J5 — When joining two vertical tables, apply the WHERE label filter on BOTH sides.

EXAMPLE JOIN:
  User: "compare industry NPA vs infra NPA"
  RIGHT:
    SELECT i.INDUSTRY_NAME, i.GROSS_NPA_END AS industry_npa, r.GROSS_NPA_END AS infra_npa
    FROM CIMS_RAQ_Q_SEC8_IND_BRKUP i
    LEFT JOIN CIMS_RAQ_Q_SEC8_INFRA_BRKUP r
      ON i.CODE = r.CODE AND i.RDATE = r.RDATE AND i.INDUSTRY_NAME = r.INDUSTRY_NAME

════════════════════════════════════════════════
MULTI-CODE / BANK RULES
════════════════════════════════════════════════
RULE B1 — Column CODE identifies the reporting bank/entity. If user does not specify a bank, omit CODE filter.
RULE B2 — If user asks for a specific bank, filter WHERE CODE = <bank_code>.

════════════════════════════════════════════════
DATE & PERIOD RULES
════════════════════════════════════════════════
RULE P1 — RDATE is the reporting date column. Use it for all date-based filtering.

RULE P2 — "Latest" / "most recent" / "current" → use a subquery:
    WHERE RDATE = (SELECT MAX(RDATE) FROM <same_table>)

RULE P3 — "Last/past/previous N quarters, months, years, FY quarters, or periods":
    If a RESOLVED TIME CONTEXT block appears below with a matching entry, use those
    EXACT dates verbatim — do not recompute them yourself.
    For "last N periods" specifically (where period = one reporting row, not a fixed
    calendar span), use the RDATE IN (SELECT DISTINCT RDATE ... ORDER BY RDATE DESC
    FETCH FIRST N ROWS ONLY) pattern shown in that block, NOT a date range.
    Only if no RESOLVED TIME CONTEXT entry covers the phrase, fall back to:
    WHERE RDATE >= ADD_MONTHS((SELECT MAX(RDATE) FROM <same_table>), -<N*3 for quarters, -N for months>)

RULE P4 — "For year YYYY" → filter with:
    WHERE EXTRACT(YEAR FROM RDATE) = YYYY

RULE P5 — "Between <date1> and <date2>" → filter with:
    WHERE RDATE BETWEEN TO_DATE('<date1>', 'YYYY-MM-DD') AND TO_DATE('<date2>', 'YYYY-MM-DD')

RULE P6 — "Trend" / "over time" / "quarter-by-quarter" → include RDATE in SELECT and GROUP BY,
    ORDER BY RDATE ASC.

RULE P7 — Never hardcode a date literal like '2024-03-31'. Always derive latest date via MAX(RDATE).

════════════════════════════════════════════════
RANKING & TOP-N RULES
════════════════════════════════════════════════
RULE R1 — "Top N" / "highest N" → use Oracle FETCH syntax:
    SELECT ... FROM ... ORDER BY <col> DESC FETCH FIRST <N> ROWS ONLY

RULE R2 — "Bottom N" / "lowest N" → ORDER BY <col> ASC FETCH FIRST <N> ROWS ONLY

RULE R3 — "Rank banks by <metric>" → use RANK() or DENSE_RANK() window function:
    SELECT CODE, <col>,
           RANK() OVER (PARTITION BY RDATE ORDER BY <col> DESC) AS rnk
    FROM ...

RULE R4 — Never use ROWNUM for top-N unless the table has no ORDER BY option; prefer FETCH FIRST.
"""


def _build_compressed_rules_block(dialect_hint: str) -> str:
    return f"""You are an expert {dialect_hint} SQL generator.
Use {dialect_hint} syntax, including Oracle-specific constructs such as FETCH FIRST N ROWS ONLY, ADD_MONTHS, EXTRACT(YEAR FROM ...), and TO_DATE(...,'YYYY-MM-DD').

════════════════════════════════════════════════
ABSOLUTE RULES
════════════════════════════════════════════════
- Return ONLY a raw SQL SELECT query: no explanation, no markdown, no code fences, no semicolon.
- Use ONLY table names and column names listed in the SCHEMA CONTEXT below.
- Never use bind variables or placeholders (:val, ?, %s); embed literals directly.
- Never touch backup tables (_bkup, _bk, _bckup, _backup suffixes).
- If a RESOLVED TIME CONTEXT entry exists for the question, use those exact dates verbatim.

════════════════════════════════════════════════
KEY GUIDELINES
════════════════════════════════════════════════
- The SCHEMA CONTEXT is the only allowed source of tables and columns.
- VERTICAL tables store named rows. Do not SUM vertical label rows across records.
- If STORAGE FORMAT: VERTICAL appears, filter by the exact label value or use LIKE only when no exact label exists.
- For DOM / OVE values, use _DOM or _OVE separately unless the user asks for combined totals.
- Use JOIN only when required, and always join on CODE and RDATE together.
- Prefer Oracle top-N syntax: ORDER BY ... FETCH FIRST N ROWS ONLY.
- Prefer Oracle date syntax: EXTRACT(YEAR FROM RDATE), TO_DATE(...,'YYYY-MM-DD'), ADD_MONTHS.
- Avoid inventing tables, columns, or business logic beyond the schema and resolved time context.
"""


def _build_minimal_prompt(
    dialect_hint: str,
    user_query: str,
    schema_context: str,
    time_context_block: str,
    valid_tables: str,
    tables=None,
    all_columns=None,
    vertical_tables=None,
    dom_ove_tables=None,
    multipart_tables=None,
) -> str:
    time_section = f"\n{time_context_block}" if time_context_block else ""
    example_blocks = []

    if vertical_tables:
        example_blocks.append(
            "Example — vertical table:\n"
            "SELECT PERIOD_DELINQUENCY, TOTAL_LOAN_ASSETS\n"
            "FROM <table>\n"
            "WHERE PERIOD_DELINQUENCY = '<total row label>'"
        )

    if dom_ove_tables:
        example_blocks.append(
            "Example — domestic/overseas table:\n"
            "SELECT CODE, RDATE, EXPOSURE_DOM, EXPOSURE_OVE\n"
            "FROM <table>"
        )

    if multipart_tables:
        example_blocks.append(
            "Example — multi-part table:\n"
            "SELECT SUM(val) FROM (SELECT value_col AS val FROM <table_a> UNION ALL SELECT value_col AS val FROM <table_b>)"
        )

    examples_section = ""
    if example_blocks:
        examples_section = "\n\n### Few-shot examples\n" + "\n\n".join(example_blocks)

    return f"""You are an expert {dialect_hint} SQL generator.
Use {dialect_hint} syntax, including Oracle-specific constructs such as FETCH FIRST N ROWS ONLY, ADD_MONTHS, EXTRACT(YEAR FROM ...), and TO_DATE(...,'YYYY-MM-DD').

### Task
{user_query}

### Database Schema
{schema_context}

Allowed tables: {valid_tables}{time_section}{examples_section}

### Important
- If a table is marked STORAGE FORMAT: VERTICAL, each row is a named metric and you must filter by the exact row label values shown above.
- Do NOT aggregate vertical tables with SUM() across row labels unless the user explicitly asks for an aggregation that requires it.
- When the user requests a total or overall value, use the exact total row label provided in the schema context.
- Never invent row labels or column names that are not shown in the schema context.

### Answer
Return ONLY a raw SQL SELECT query. No explanation, no markdown, no code fences, no semicolon.
"""


# Keywords in row-label values that indicate a pre-aggregated total row
_TOTAL_ROW_KEYWORDS = [
    "total", "grand total", "sub-total", "subtotal",
    "all industries", "c. total", "c total", "grand-total",
    "i. gross", "iii. non-food", "ii. food",
]


def _find_total_row(values: list) -> str | None:
    """Return the first value in the list that looks like a total/grand-total row."""
    for v in values:
        vl = v.lower()
        if any(kw in vl for kw in _TOTAL_ROW_KEYWORDS):
            return v
    return None


def build_prompt(user_query, tables, columns, dialect="Oracle", today_date=None, matched_labels=None, model_name=None):
    if today_date is None:
        today_date = date.today().isoformat()

    if model_name is None:
        model_name = config.OLLAMA_MODEL
    profile = _get_model_profile(model_name)
    dialect_hint = profile.get("dialect_hint") or dialect
    prompt_style = profile.get("prompt_style", "rules")
    supports_full_ruleset = profile.get("supports_full_ruleset", True)

    table_names = {t["table"] for t in tables}
    all_columns = _load_all_columns(table_names)

    # ── Build per-table label lookup from matched_labels (L2 embeddings) ──────
    # matched_labels: [{table, column, value}, ...]  — already ranked by relevance
    # Fall back to full sample dump if index doesn't exist yet (e.g. first run).
    if matched_labels is None:
        from src.description_fetcher import load_samples
        raw_samples = load_samples()
        matched_labels = []
        for tbl, col_map in raw_samples.items():
            for col, vals in col_map.items():
                for v in vals:
                    matched_labels.append({"table": tbl, "column": col, "value": v})

    # Group matched labels by (table, column) for easy lookup in prompt builder
    from collections import defaultdict
    label_map = defaultdict(lambda: defaultdict(list))  # label_map[table][col] = [val, …]
    for lbl in matched_labels:
        table_name = lbl["table"]
        column_name = lbl["column"]
        if prompt_style == "minimal":
            if len(label_map[table_name][column_name]) < MAX_LABELS_MINIMAL:
                label_map[table_name][column_name].append(lbl["value"])
        else:
            label_map[table_name][column_name].append(lbl["value"])

    # ── Supplement with ALL known samples for matched tables ──────────────────
    # Semantic search only returns top-K matches; for tables with few rows (≤50),
    # we want the LLM to see every known value so it never has to guess/hallucinate.
    # This only applies to the richer rules-style prompt so minimal prompts stay concise.
    if prompt_style == "rules":
        from src.description_fetcher import load_samples as _load_samples
        all_samples = _load_samples()
        for tbl in table_names:
            if tbl in all_samples:
                for col, vals in all_samples[tbl].items():
                    existing = set(label_map[tbl][col])
                    for v in vals:
                        if v not in existing:
                            label_map[tbl][col].append(v)

    vertical_tables = set()
    dom_ove_tables = set()
    multipart_tables = set()
    for t in tables:
        table_name = t["table"]
        table_upper = table_name.upper()
        if table_name in label_map and label_map[table_name]:
            vertical_tables.add(table_name)
        if any(re.search(r'(_dom|_ove)\b', c["column"].upper()) for c in all_columns if c["table"].upper() == table_upper):
            dom_ove_tables.add(table_name)
        if re.search(r'_part_[ab](?:_|$)', table_name.lower()):
            multipart_tables.add(table_name)

    schema_lines = []
    for t in tables:
        table_name = t["table"].upper()
        table_cols = [c["column"].upper() for c in all_columns if c["table"].upper() == table_name]
        cols_str = ", ".join(table_cols) if table_cols else "(none)"
        block = f"Table: {table_name}\nAllowed columns (use ONLY these): {cols_str}"

        col_labels = label_map.get(t["table"], {})
        if col_labels:
            block += "\nSTORAGE FORMAT: VERTICAL — each row is a named metric. DO NOT aggregate with SUM() across all rows."
            label_lines = []
            for col, values in col_labels.items():
                sample_str = ", ".join(f"'{v}'" for v in values)
                label_lines.append(f"  {col.upper()} relevant values: {sample_str}")

                total_row = _find_total_row(values)
                if total_row:
                    label_lines.append(
                        f"  *** TOTAL ROW for {col.upper()}: '{total_row}' — "
                        f"use WHERE {col.upper()} = '{total_row}' when user asks for totals/overall/grand total ***"
                    )

            block += "\nRelevant row labels (matched to your query):\n" + "\n".join(label_lines)

        schema_lines.append(block)

    schema_context = "\n\n".join(schema_lines)
    valid_tables = ", ".join(t["table"].upper() for t in tables)

    today_obj = date.fromisoformat(today_date) if isinstance(today_date, str) else today_date
    _time_block = _resolve_relative_time(user_query, today_obj)
    time_context_block = (_time_block + "\n") if _time_block else ""

    if prompt_style == "minimal":
        return _build_minimal_prompt(
            dialect_hint,
            user_query,
            schema_context,
            time_context_block,
            valid_tables,
            tables=tables,
            all_columns=all_columns,
            vertical_tables=vertical_tables,
            dom_ove_tables=dom_ove_tables,
            multipart_tables=multipart_tables,
        )

    template = _build_full_rules_block(dialect_hint) if supports_full_ruleset else _build_compressed_rules_block(dialect_hint)
    return f"""{template}
════════════════════════════════════════════════
SCHEMA CONTEXT
════════════════════════════════════════════════
{schema_context}

════════════════════════════════════════════════
Allowed tables: {valid_tables}
{time_context_block}User question: {user_query}
════════════════════════════════════════════════
SQL:"""

def generate_sql(user_query, tables, columns, dialect="Oracle", today_date=None, matched_labels=None):
    prompt = build_prompt(user_query, tables, columns, dialect=dialect, today_date=today_date, matched_labels=matched_labels)
    model_name = config.OLLAMA_MODEL
    model_profile = _get_model_profile(model_name)

    table_names = [str((t.get("table") or t.get("table_name") or "")).strip() for t in tables]
    needs_multi_part_output = any(re.search(r'_part_[ab](?:_|$)', name.lower()) for name in table_names if name)
    effective_num_predict = model_profile.get("num_predict")
    if model_profile.get("prompt_style") == "minimal" and needs_multi_part_output:
        effective_num_predict = max(effective_num_predict or 0, config.MINIMAL_MULTIPART_NUM_PREDICT)

    def _call_ollama(prompt_text: str):
        payload = {
            "model": model_name,
            "prompt": prompt_text,
            "stream": True,   # stream tokens as they arrive
        }
        options = {}
        if model_profile.get("temperature") is not None:
            options["temperature"] = model_profile["temperature"]
        if effective_num_predict is not None:
            options["num_predict"] = effective_num_predict
        if options:
            payload["options"] = options

        try:
            response = requests.post(config.OLLAMA_URL, json=payload, timeout=300, stream=True)
            response.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                "Cannot connect to Ollama. Make sure it is running: `ollama serve`"
            )
        except requests.exceptions.ReadTimeout:
            raise RuntimeError(
                "Ollama timed out (300s). Try a smaller/faster model in config.py."
            )
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Ollama API error: {e}")

        print("  ", end="", flush=True)
        raw = ""
        for line in response.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            token = chunk.get("response", "")
            print(token, end="", flush=True)
            raw += token
            if chunk.get("done"):
                break
        print()
        return raw

    print(f"[DEBUG] Ollama model={model_name} prompt_style={model_profile.get('prompt_style')} supports_full_ruleset={model_profile.get('supports_full_ruleset')}" )
    raw = _call_ollama(prompt)

    # Strip markdown fences if present
    raw = re.sub(r'^```(?:sql)?\s*', '', raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r'```\s*$', '', raw).strip()
    # Oracle driver rejects trailing semicolons
    raw = raw.rstrip().rstrip(";")

    is_valid, reason = validate_sql(raw, tables, columns)
    warnings = []

    if not is_valid:
        retry_prompt = (
            "The previous SQL was invalid. Return ONLY the corrected SQL.\n\n"
            f"Original prompt:\n{prompt}\n\n"
            f"Invalid SQL:\n{raw}\n\n"
            f"Validation reason:\n{reason}\n\n"
            "Corrected SQL:"
        )
        raw = _call_ollama(retry_prompt)
        raw = re.sub(r'^```(?:sql)?\s*', '', raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r'```\s*$', '', raw).strip()
        raw = raw.rstrip().rstrip(";")
        is_valid, reason = validate_sql(raw, tables, columns)

    if not is_valid:
        category = _validation_failure_category(reason)
        warning = (
            f"Model '{model_name}' generated invalid SQL; probable failure category: {category}. "
            f"Reason: {reason}"
        )
        warnings.append(warning)
        print(f"[WARNING] {warning}")

    return {
        "question_understanding": "",
        "sql": raw,
        "result_columns": [],
        "visualizations": [],
        "followup_questions": [],
        "warnings": warnings,
    }


def validate_sql(sql, tables, columns):
    """Returns (is_valid: bool, reason: str)"""
    if isinstance(sql, dict):
        sql = sql.get("sql", "")

    if not sql:
        return False, "Empty SQL"
    # Normalise: strip quotes, lowercase
    q = sql.lower().replace('"', '').replace("'", '').strip()

    # 1 — must start with SELECT
    if not q.startswith("select"):
        return False, "Only SELECT queries are allowed"

    # 2 — no dangerous DML/DDL keywords (whole-word match)
    for word in BANNED_KEYWORDS:
        if re.search(rf'\b{word}\b', q):
            return False, f"Dangerous keyword detected: '{word}'"

    valid_table_names = {t["table"].lower() for t in tables}

    # 3 — load ALL real columns for matched tables for accurate validation
    all_columns = _load_all_columns(valid_table_names)
    valid_col_names = {c["column"].lower() for c in all_columns}

    # Collect aliases defined inside subqueries / CTEs — these are valid even
    # though they don't appear in the schema (e.g. SUM(...) AS industry_npa)
    subquery_aliases = set(re.findall(r'\bas\s+([a-z_][a-z0-9_]*)', q))
    # Also collect subquery range variables  (... ) alias_name)
    subquery_aliases |= set(re.findall(r'\)\s+([a-z_][a-z0-9_]*)\b', q))

    # 4 — every table in FROM / JOIN must be an allowed table OR a subquery alias
    # Strip EXTRACT(... FROM ...) and TRIM(... FROM ...) before scanning for
    # table references — otherwise "FROM RDATE" inside EXTRACT is mistakenly
    # treated as a table name, causing false "hallucinated table" errors.
    q_for_tables = re.sub(r'\bextract\s*\([^)]*\)', '', q)
    q_for_tables = re.sub(r'\btrim\s*\([^)]*\)', '', q_for_tables)
    referenced_tables = set(re.findall(r'(?:from|join)\s+([a-z_][a-z0-9_]*)', q_for_tables))
    real_table_refs = referenced_tables - subquery_aliases
    hallucinated_tables = real_table_refs - valid_table_names
    if hallucinated_tables:
        return False, f"Hallucinated tables (not in schema): {sorted(hallucinated_tables)}"

    # 5 — at least one allowed table must appear
    if not referenced_tables & valid_table_names:
        return False, f"Query does not reference any matched table: {sorted(valid_table_names)}"

    # 6 — check outermost SELECT-list columns only
    select_body = re.split(r'\bfrom\b', q_for_tables, maxsplit=1)[0]
    select_body = select_body.replace("select", "", 1).strip()

    # remove aliases (AS xxx)
    select_body = re.sub(r'\bas\s+[a-z_][a-z0-9_]*', '', select_body)
    # remove aggregate / scalar functions
    select_body = re.sub(r'\b(sum|avg|min|max|count|coalesce|nvl|nullif|trim|upper|lower|to_date|to_char)\s*\(', '(', select_body)
    # remove math operators and parens
    select_body = re.sub(r'[*/+\-()\[\]]', ' ', select_body)

    col_tokens = re.findall(r'(?:[a-z_][a-z0-9_]*\.)?([a-z_][a-z0-9_]*)', select_body)

    hallucinated_cols = {
        t for t in col_tokens
        if (
            t not in valid_col_names
            and t not in _SQL_KEYWORDS
            and t not in subquery_aliases     # ← skip aliases defined in subqueries
            and t != "*"
            and not t.isdigit()
            and len(t) > 2
        )
    }

    if hallucinated_cols:
        return False, f"Hallucinated columns (not in schema): {sorted(hallucinated_cols)}"

    return True, "Valid"