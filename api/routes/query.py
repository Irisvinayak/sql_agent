import re
import time
import logging

from fastapi import APIRouter, HTTPException

from api.schemas import QueryRequest, QueryResult
from api.utils import serialize_rows
from src import config
from src.executor import execute_query
from src.frequency_alias import apply_frequency_override
from src.retriever import get_relevant_schema, find_exact_qa_match, compute_query_embedding
from src.selector import select_tables
from src.semantic_layer import load_join_graph
from src.sql_generator import generate_sql, validate_sql

log = logging.getLogger("query")

router = APIRouter(tags=["query"])

MIN_QUERY_LENGTH = 20

_TIME_PATTERNS = [
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    r"\b(january|february|march|april|june|july|august|september|october|november|december)\b",
    r"\b(q[1-4]|quarter)\b",
    r"\b(20\d{2}|19\d{2})\b",
    r"\b(fy|financial year|fiscal year)\b",
    r"\b(last|this|current|previous)\s+(month|year|quarter|week)\b",
    r"\b(ytd|mtd|year[\s-]to[\s-]date|month[\s-]to[\s-]date)\b",
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
    r"\b(period|as of|ending|ended|as at)\b",
    r"\b(h[12]|half year|half-year)\b",
]


def _has_time_context(query: str) -> bool:
    q = query.lower()
    return any(re.search(p, q) for p in _TIME_PATTERNS)


def _empty_result(query: str, hint: str) -> QueryResult:
    return QueryResult(
        query=query,
        matched_tables=[],
        matched_columns=[],
        sql="",
        is_valid=False,
        validation_reason=None,
        columns=[],
        rows=[],
        needs_more_info=True,
        more_info_hint=hint,
    )


@router.post("/query", response_model=QueryResult)
def run_text_query(body: QueryRequest):
    q = body.query.strip()
    log.info("QUERY   : %r", q)  # the raw text was never logged before this — impossible to debug a "why didn't this match" report without it
    _t0 = time.perf_counter()
    timings_ms: dict = {}

    def _mark(stage: str):
        nonlocal _t0
        now = time.perf_counter()
        timings_ms[stage] = round((now - _t0) * 1000, 1)
        _t0 = now

    # Guard 1 — minimum query length
    if len(q) < MIN_QUERY_LENGTH:
        return _empty_result(
            q,
            "Please provide more details about what data you need. "
            "Describe the report or metric you want, and include a time period "
            "(e.g. Q1 FY2024, March 2025, FY 2023-24, or a specific date).",
        )

    # Soft hint — no time duration detected; run query but suggest refinement
    _accuracy_hint = None
    if not _has_time_context(q):
        _accuracy_hint = (
            "Tip: For more accurate results, try specifying a time period — "
            "e.g. a specific date (01-Mar-2025), month (March 2025), "
            "quarter (Q1 FY2024), half-year (H1 2024), year (2024), "
            "or a date range (Jan–Jun 2025)."
        )

    # Step 0 — BEFORE any table/column shortlisting: check qa_pairs.json for a
    # near-identical stored question (>=99% literal text match to what the
    # user typed). If found, there's nothing to retrieve or generate — the
    # answer was already hand-verified when that qa_pairs.json entry was
    # written, so just reuse it directly. Faster, fully deterministic, zero
    # hallucination risk.
    # Embed the query ONCE and reuse it for both the exact-match lookup and
    # full retrieval below — previously each of these (and the 4 signals
    # inside get_relevant_schema) called embed_query() independently,
    # meaning a single request could pay for ~6 redundant model.encode()
    # calls (~100ms each on CPU) for the exact same text.
    query_vec = compute_query_embedding(q)

    exact = find_exact_qa_match(q, query_vec=query_vec)
    _mark("exact_match_lookup")
    if exact:
        log.info("EXACT QA MATCH (text_similarity=%.3f): %s", exact["text_similarity"], exact["question"])
        sql = exact["sql"]
        exact_table = exact["table"]

        tables = [{"table": exact_table}]
        is_valid, reason = validate_sql(sql, tables, [])
        _mark("validation")
        log.info("VALID   : %s  reason=%s", is_valid, reason)

        # If the user explicitly named a frequency (monthly/quarterly/daily/
        # fortnightly/annual) different from this stored example's own table,
        # swap in that frequency's real sibling table — discovered dynamically
        # from the live Oracle catalog (src/frequency_alias.py) and verified
        # with a live Oracle dry-run before being trusted, since schema.json
        # never has column info for a sibling table (only the canonical/
        # embedded table this SQL was just validated against). MUST run only
        # AFTER validate_sql above, never before: validating against the
        # sibling table name directly would flag every real column as
        # hallucinated (schema.json has no entry for it at all).
        freq_warning = None
        if is_valid:
            sql, exact_table, freq_warning = apply_frequency_override(exact_table, sql, q)
            tables = [{"table": exact_table}]
        _mark("frequency_resolution")

        col_names, rows, db_error = [], [], None
        if is_valid:
            col_names, rows, db_error = execute_query(sql)
            log.info("DB COLS : %s", col_names)
            log.info("DB ROWS : %d row(s) returned", len(rows))
            if db_error:
                log.error("DB ERR  : %s", db_error)
        _mark("db_execution")
        log.info("TIMINGS : %s  (total=%.1fms)", timings_ms, sum(timings_ms.values()))

        return QueryResult(
            query=q,
            matched_tables=[exact_table],
            matched_columns=[],
            sql=sql,
            is_valid=is_valid,
            validation_reason=reason if not is_valid else None,
            columns=col_names,
            rows=serialize_rows(rows),
            db_error=db_error,
            source="direct_match",
            match_score=exact["text_similarity"],
            timings_ms=timings_ms,
            warnings=[freq_warning] if freq_warning else [],
        )

    # Step 1 — schema retrieval (L1: tables/columns  L2: row-label values  L3: qa example
    # — a 95%+ literal-text match here is already guaranteed to be the first
    # table in the list, and passed through as a strong few-shot hint below)
    # Retrieval runs wide (SHORTLIST_K) for recall; the selector below narrows it
    # to the one table that goes in the prompt. Handing the whole shortlist to the
    # SQL model is what produced the fabricated-join failures.
    retrieval = get_relevant_schema(
        q, query_vec=query_vec, shortlist_k=config.SHORTLIST_K,
    )
    tables, columns = retrieval.tables, retrieval.columns
    matched_labels, qa_example = retrieval.matched_labels, retrieval.qa_example
    _mark("retrieval")
    log.info("SHORTLIST: %s", [t["table"] for t in tables])
    log.info("COLUMNS : %s", [f"{c['table']}.{c['column']}" for c in columns])

    if not tables:
        return QueryResult(
            query=q,
            matched_tables=[],
            matched_columns=[],
            sql="",
            is_valid=False,
            validation_reason="No matching tables found in the vector index.",
            columns=[],
            rows=[],
            accuracy_hint=_accuracy_hint,
        )

    # Step 1b — selection: shortlist → the table(s) that actually reach the SQL
    # model. Never raises; falls back to the top-1 retrieved table.
    tables, selection = select_tables(
        q, tables, matched_labels=matched_labels, join_graph=load_join_graph(),
    )
    _mark("selection")
    log.info("SELECTED: %s%s", [t["table"] for t in tables],
             "" if selection else "  (short-circuit, no LLM call)")

    # Row labels and columns were gathered for the whole shortlist — drop the
    # ones belonging to tables the selector rejected, so nothing from a
    # discarded table leaks into the prompt.
    selected_names = {t["table"] for t in tables}
    columns = [c for c in columns if c["table"] in selected_names]
    matched_labels = [l for l in matched_labels if l["table"] in selected_names]

    # Step 2 — SQL generation. Any qa_pairs.json match at 95%+ literal text
    # similarity was already forced to the front of `tables` above and will
    # be passed to the model as a strong few-shot example below — this is the
    # "still call the LLM, but ground it hard" tier.
    try:
        result = generate_sql(q, tables, columns, dialect=body.dialect,
                              matched_labels=matched_labels, qa_example=qa_example,
                              selection=selection)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    _mark("llm_generation")

    sql = result.get("sql", "")
    log.info("SQL     :\n%s", sql)
    source = "llm_generated"
    match_score = max(qa_example["text_similarity"], qa_example["token_similarity"]) if qa_example else None

    # Step 3 — validation. generate_sql() already ran this exact SQL through
    # validate_sql() (and an Oracle dry-run) as the last step of its own
    # retry loop and returns the result — reuse it instead of re-running the
    # same regex checks a second time on SQL that was just validated.
    is_valid, reason = result.get("is_valid"), result.get("validation_reason")
    _mark("validation")
    log.info("VALID   : %s  reason=%s", is_valid, reason)

    # Step 3b — frequency resolution: if the question explicitly names a
    # reporting frequency (monthly/quarterly/daily/fortnightly/annual)
    # different from the selected table's own, swap in that frequency's real
    # sibling table (discovered dynamically from the live Oracle catalog,
    # verified with a live Oracle dry-run — see src/frequency_alias.py). MUST
    # run only AFTER generate_sql/validate_sql above, never before: those
    # validate columns against `tables[0]`'s schema.json entry, which never
    # exists for a sibling table (only the canonical/embedded table generation
    # actually used) — validating against the sibling directly would flag
    # every real column as hallucinated.
    freq_warning = None
    if is_valid and tables:
        canonical_table = tables[0]["table"]
        sql, resolved_table, freq_warning = apply_frequency_override(canonical_table, sql, q)
        if resolved_table != canonical_table:
            tables[0] = {**tables[0], "table": resolved_table}
    _mark("frequency_resolution")

    # Step 4 — execution (only if valid)
    col_names, rows, db_error = [], [], None
    if is_valid:
        col_names, rows, db_error = execute_query(sql)
        log.info("DB COLS : %s", col_names)
        log.info("DB ROWS : %d row(s) returned", len(rows))
        if db_error:
            log.error("DB ERR  : %s", db_error)
    _mark("db_execution")
    log.info("TIMINGS : %s  (total=%.1fms)", timings_ms, sum(timings_ms.values()))

    return QueryResult(
        query=q,
        matched_tables=[t["table"] for t in tables],
        matched_columns=[f"{c['table']}.{c['column']}" for c in columns],
        sql=sql,
        is_valid=is_valid,
        validation_reason=reason if not is_valid else None,
        columns=col_names,
        rows=serialize_rows(rows),
        db_error=db_error,
        accuracy_hint=_accuracy_hint,
        source=source,
        match_score=match_score,
        timings_ms=timings_ms,
        warnings=result.get("warnings", []) + ([freq_warning] if freq_warning else []),
    )
