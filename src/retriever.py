import os
import re
import difflib
import faiss
import pickle
import numpy as np
from src.vectorizer import embed_query
import src.config as config
from src.config import TOP_K_TABLES, TOP_K_COLUMNS

TOP_K_LABELS = 10   # max row-label values to retrieve per query

# Minimum cosine similarity score to accept a FAISS result (IndexFlatIP,
# vectors are L2-normalised so dot-product == cosine similarity).
MIN_TABLE_SCORE  = 0.25
MIN_COLUMN_SCORE = 0.20

# Minimum similarity for a qa_index hit to be trusted as a concrete few-shot
# example in the SQL prompt (higher bar than table retrieval — a weak match
# here would actively mislead the LLM rather than just miss a table).
QA_EXAMPLE_MIN_SCORE = 0.75

# Two LITERAL TEXT similarity (0-1) tiers for a qa_pairs.json match, checked
# against the user's raw query. Deliberately NOT based on the embedding
# cosine score: bge-large-en's asymmetric query/passage encoding means even a
# word-for-word identical question can score anywhere from ~0.79 to ~0.95
# depending on length/content, and an unrelated-but-topically-similar question
# can land in that same range — so cosine score alone cannot safely
# distinguish "this IS the same question" from "this just sounds similar".
# Literal text similarity has no such ambiguity.
#
#   >= EXACT_MATCH_MIN_RATIO   -> the user typed essentially the SAME sentence
#                                 as a stored question. Skip the LLM entirely,
#                                 execute the verified stored SQL directly.
#   >= STRONG_MATCH_MIN_RATIO  -> close but not identical. Still call the LLM,
#                                 but ground it hard with this near-exact
#                                 example instead of letting it guess.
EXACT_MATCH_MIN_RATIO = 0.99
STRONG_MATCH_MIN_RATIO = 0.95


def _normalize_for_match(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip().lower())


def text_similarity(a: str, b: str) -> float:
    """Character-level similarity ratio (0-1) between two strings, whitespace/case-insensitive."""
    return difflib.SequenceMatcher(None, _normalize_for_match(a), _normalize_for_match(b)).ratio()


# Generic FUNCTION words only — no distinguishing meaning for "is this the
# same question" — stripped before token comparison so two questions match
# on their actual CONTENT words (not just "what"/"the"/"is"). Deliberately
# does NOT include "raq"/"cims"/etc: those are return/product identifiers,
# not filler — stripping them would make "for RAQ" and "for RBS" (or any
# other real return) look identical once this scope covers more than one
# return, erasing exactly the token that distinguishes them.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "what", "how", "of", "for",
    "to", "in", "on", "and", "or", "show", "me", "give", "tell", "please",
    "does", "do", "this", "that", "it", "its", "much", "many",
}

# Light suffix-stripping (not a real stemmer — no NLTK/spaCy dependency
# needed for this) so "advance"/"advances", "expose"/"exposure"-style
# plural/inflection differences don't count as a mismatch.
_STEM_SUFFIXES = ("ing", "ed", "es", "s")


def _stem(token: str) -> str:
    for suf in _STEM_SUFFIXES:
        if len(token) > len(suf) + 2 and token.endswith(suf):
            return token[: -len(suf)]
    return token


def _tokenize(text: str) -> set:
    # Route through _expand_query first so domain abbreviations (dom/ove,
    # npa, raq, sec1/sec2...) normalize the same way on both sides of the
    # comparison — a user typing "dom" and a stored question saying
    # "domestic" should count as the same token, not a mismatch.
    words = re.findall(r'[a-z0-9]+', _expand_query(text).lower())
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 2}


def token_similarity(a: str, b: str) -> float:
    """
    Token-set (Jaccard) similarity after lowercasing, domain-abbreviation
    expansion, stopword removal, and light suffix-stripping. This catches
    real paraphrases that character-level text_similarity misses — reordered
    clauses, plurals, "closing balance of X" vs "X at the end of the
    period" — without the unpredictable scoring range of raw embedding
    cosine similarity (see the note on EXACT_MATCH_MIN_RATIO above).

    Deliberately NOT used to gate the skip-the-LLM tier: word-overlap can be
    fooled by a meaning-changing swap that still shares most tokens (e.g.
    "domestic" vs "overseas", "Part A" vs "Part B") — safe enough to
    guarantee a table is worth showing the model, not safe enough to bypass
    the model's judgment entirely.
    """
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def best_qa_similarity(a: str, b: str) -> float:
    """The stronger of literal text similarity and token-set similarity — use
    for the "should we ground/prioritize this table" decision, never for the
    "skip the LLM entirely" decision (see token_similarity's docstring)."""
    return max(text_similarity(a, b), token_similarity(a, b))


def find_exact_qa_match(query: str):
    """
    Cheap, standalone check run BEFORE any table/column shortlisting: look up
    only the qa_index (question -> gold SQL) for a near-identical stored
    question. If the user typed essentially the same sentence as one already
    in qa_pairs.json, there's nothing to retrieve or generate — just reuse
    the verified answer.

    Returns {question, sql, table, text_similarity} or None.
    """
    hits = search_qa(_expand_query(query), top_k=1)
    if not hits:
        return None
    _score, qa = hits[0]
    sim = text_similarity(query, qa["question"])
    if sim >= EXACT_MATCH_MIN_RATIO:
        return {"question": qa["question"], "sql": qa["sql"], "table": qa["table"], "text_similarity": sim}
    return None


_fallback_map_cache: dict = {}  # keyed by EMBEDDING_DIR, so a scope switch mid-session doesn't reuse a stale map


def _load_fallback_map():
    """
    Some Annual-return tables in test/schema.json (derived from the XBRL
    taxonomy) don't actually exist in Oracle yet — only their Quarterly
    counterpart does. This maps annual table name -> existing quarterly
    table name, built by test/build_test_index.py's sibling fallback script.
    Returns {} if the file doesn't exist (e.g. production mode).
    """
    embedding_dir = config.EMBEDDING_DIR
    if embedding_dir not in _fallback_map_cache:
        import json
        path = f"{embedding_dir}/annual_to_quarterly_fallback.json"
        try:
            with open(path, encoding="utf-8") as f:
                _fallback_map_cache[embedding_dir] = json.load(f)
        except FileNotFoundError:
            _fallback_map_cache[embedding_dir] = {}
    return _fallback_map_cache[embedding_dir]


def _apply_quarterly_fallback(tables):
    """
    Replace any table not present in the live DB with its Quarterly
    equivalent (if known), tagging the substitution so callers can surface
    a note to the user. Column loading falls back to the production schema
    for these substituted names (see sql_generator._load_all_columns).
    """
    fallback_map = _load_fallback_map()
    if not fallback_map:
        return tables

    resolved = []
    for t in tables:
        original = t["table"]
        quarterly = fallback_map.get(original)
        if quarterly:
            resolved.append({**t, "table": quarterly, "fallback_from": original})
        else:
            resolved.append(t)
    return resolved

# ── Banking / CIMS domain abbreviation expansion ──────────────────────────────
_QUERY_EXPANSIONS = [
    (r'\bnpa\b',    'NPA non performing assets'),
    (r'\bgnpa\b',   'gross NPA non performing assets'),
    (r'\bnnpa\b',   'net NPA non performing assets'),
    (r'\bsma\b',    'special mention accounts SMA'),
    (r'\bcar\b',    'capital adequacy ratio CAR'),
    (r'\bpcr\b',    'provision coverage ratio PCR'),
    (r'\brwa\b',    'risk weighted assets RWA'),
    (r'\bslr\b',    'statutory liquidity ratio SLR'),
    (r'\bcrr\b',    'cash reserve ratio CRR'),
    (r'\bpsl\b',    'priority sector lending PSL'),
    (r'\braq\b',    'Risk Assessment Questionnaire RAQ CIMS'),
    (r'\bcims\b',   'CIMS banking supervisory return'),
    (r'\bsec(\d+)\b', r'section \1'),
    (r'\bdom\b',    'domestic'),
    (r'\bove\b',    'overseas'),
    (r'\binfra\b',  'infrastructure'),
    (r'\bsensec\b', 'sensitive sector'),
    (r'\bparta\b',  'part A'),
    (r'\bpartb\b',  'part B'),
]


def _expand_query(query: str) -> str:
    """Expand banking abbreviations so the embedding model understands them."""
    q = query
    for pattern, replacement in _QUERY_EXPANSIONS:
        q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
    return q


def _dynamic_top_k(query: str) -> int:
    """
    Return a higher TOP_K only for queries with genuine multi-section signals
    (explicit section/part references, or comparison language). Generic
    conjunctions like "and"/"both"/"all" were previously counted too — those
    appear in almost any single-topic question ("show X and Y from this
    return") and caused routine over-widening, which let unrelated tables
    fill the extra slots via a single weak column/label match.
    """
    hits = len(re.findall(
        r'\b(sec\d+|part\s*[ab]|section\s*\d+|compare|versus|vs)\b',
        query, re.IGNORECASE,
    ))
    if hits >= 3:
        return TOP_K_TABLES + 3
    if hits >= 1:
        return TOP_K_TABLES + 1
    return TOP_K_TABLES


def search(index_path, meta_path, query, k, min_score=0.0):
    """Search a FAISS index, returning only hits above min_score."""
    index = faiss.read_index(index_path)

    with open(meta_path, "rb") as f:
        meta = pickle.load(f)

    if not meta:
        return []

    effective_k = min(k, len(meta))
    q_vec = np.array([embed_query(query)]).astype("float32")
    distances, indices = index.search(q_vec, effective_k)

    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx != -1 and dist >= min_score:
            results.append((float(dist), meta[idx]))
    return results   # list of (score, meta_dict)


def _rrf(rank: int, k: int = 60) -> float:
    """Reciprocal Rank Fusion score."""
    return 1.0 / (k + rank + 1)


def search_qa(query: str, top_k: int = 15):
    """
    Search the table_qa.json-derived question index (test/qa_index.faiss),
    built by test/build_test_index.py. Returns [] if it hasn't been built.
    """
    qa_index_path = f"{config.EMBEDDING_DIR}/qa_index.faiss"
    qa_meta_path = f"{config.EMBEDDING_DIR}/qa_meta.pkl"
    if not os.path.exists(qa_index_path):
        return []
    return search(qa_index_path, qa_meta_path, query, top_k, min_score=MIN_TABLE_SCORE)


def get_relevant_schema(query: str):
    expanded = _expand_query(query)
    top_k = _dynamic_top_k(query)
    embedding_dir = config.EMBEDDING_DIR

    # ── Signal A: direct table semantic search ────────────────────────────────
    table_hits = search(
        f"{embedding_dir}/table_index.faiss", f"{embedding_dir}/table_meta.pkl",
        expanded, top_k * 3, min_score=MIN_TABLE_SCORE,
    )

    # ── Signal B: column search → which tables do best columns belong to? ─────
    col_hits = search(
        f"{embedding_dir}/column_index.faiss", f"{embedding_dir}/column_meta.pkl",
        expanded, TOP_K_COLUMNS * 6, min_score=MIN_COLUMN_SCORE,
    )

    # ── Signal C: row-label search → which tables do best labels belong to? ───
    from src.description_fetcher import search_labels_with_scores
    label_hits = search_labels_with_scores(expanded, top_k=TOP_K_LABELS * 3)

    # ── Signal D: question→SQL example search (test/qa_index.faiss, optional) ─
    qa_hits = search_qa(expanded, top_k=top_k * 3)

    # ── RRF: fuse all 3 signals into a single table ranking ───────────────────
    # Different sources can disagree on table-name casing (schema.json-derived
    # indexes store lowercase; a hand-written qa_pairs.json might use
    # uppercase) — fuse on a normalised (uppercase) key everywhere so the same
    # real table never gets split into two separate score-dict entries.
    def _norm(name: str) -> str:
        return name.upper()

    all_table_meta = {_norm(h["table"]): h for _, h in table_hits}
    scores: dict[str, float] = {tbl: 0.0 for tbl in all_table_meta}

    # Signal A weight = 2.0 (most direct)
    for rank, (_, t) in enumerate(table_hits):
        key = _norm(t["table"])
        scores[key] = scores.get(key, 0.0) + _rrf(rank) * 2.0

    # Signal B weight = 1.5 (strong: column names are very specific)
    col_table_seen: dict[str, int] = {}
    for _, c in col_hits:
        tbl = _norm(c["table"])
        if tbl not in col_table_seen:
            col_table_seen[tbl] = 0
        rank = col_table_seen[tbl]
        col_table_seen[tbl] += 1
        if tbl in scores:
            scores[tbl] += _rrf(rank) * 1.5
        else:
            # Column matched a table the table-search missed — add it
            all_table_meta[tbl] = {"table": c["table"]}
            scores[tbl] = _rrf(rank) * 1.5

    # Signal C weight = 1.0
    label_table_seen: dict[str, int] = {}
    for _, lbl in label_hits:
        tbl = _norm(lbl["table"])
        if tbl not in label_table_seen:
            label_table_seen[tbl] = 0
        rank = label_table_seen[tbl]
        label_table_seen[tbl] += 1
        if tbl in scores:
            scores[tbl] += _rrf(rank) * 1.0
        else:
            all_table_meta[tbl] = {"table": lbl["table"]}
            scores[tbl] = _rrf(rank) * 1.0

    # Signal D weight = 2.5 (question→SQL example match is the strongest signal)
    qa_table_seen: dict[str, int] = {}
    for _, qa in qa_hits:
        tbl = _norm(qa["table"])
        if tbl not in qa_table_seen:
            qa_table_seen[tbl] = 0
        rank = qa_table_seen[tbl]
        qa_table_seen[tbl] += 1
        if tbl in scores:
            scores[tbl] += _rrf(rank) * 2.5
        else:
            all_table_meta[tbl] = {"table": qa["table"]}
            scores[tbl] = _rrf(rank) * 2.5

    # RRF only considers rank, not raw similarity magnitude — so a near-exact
    # paraphrase match (e.g. 0.895) and a mediocre one (e.g. 0.866) get almost
    # the same vote if they land at rank 0 vs rank 1. In this schema, table
    # descriptions are all worded similarly ("risk category, domestic vs
    # overseas, outstanding amount..."), so raw table/column cosine scores
    # cluster tightly and rarely discriminate well on their own — but a close
    # match against a REAL worded question (qa_hits) is a much stronger
    # confidence signal. Give the top QA hit's table a decisive bonus scaled
    # by how far above the "trustworthy example" bar it scores, so a
    # confident question match can override a crowd of similarly-scored but
    # actually-unrelated tables instead of being diluted into one more vote.
    if qa_hits:
        top_qa_score, top_qa = qa_hits[0]
        if top_qa_score >= QA_EXAMPLE_MIN_SCORE:
            bonus_tbl = _norm(top_qa["table"])
            bonus = (top_qa_score - QA_EXAMPLE_MIN_SCORE) * 5.0
            scores[bonus_tbl] = scores.get(bonus_tbl, 0.0) + bonus
            all_table_meta.setdefault(bonus_tbl, {"table": top_qa["table"]})

    # Pick top_k tables by fused score, but drop any table that only scraped
    # in via a single weak signal while a clearly dominant table exists —
    # otherwise a widened top_k slot gets filled by an unrelated table on the
    # strength of one stray column/label hit (e.g. a shared _DOM/_OVE column
    # name pulling in a table from a completely different section).
    ranked_all = sorted(scores, key=scores.__getitem__, reverse=True)
    if ranked_all:
        top_score = scores[ranked_all[0]]
        RELATIVE_FLOOR = 0.15  # keep tables scoring at least this fraction of the top hit
        ranked = [t for t in ranked_all if scores[t] >= top_score * RELATIVE_FLOOR][:top_k]
    else:
        ranked = []
    tables = [all_table_meta[tbl] for tbl in ranked]

    # ── Substitute any Annual table missing from the live DB with its
    #    existing Quarterly equivalent (see test/annual_to_quarterly_fallback.json) ──
    # Two different Annual tables can map to the same Quarterly table (e.g.
    # SEC11_COUNTRY_RISK and SEC11_COUNTRY_RISK_TL both -> Q_SEC11), so dedupe
    # by the resolved table name afterward — otherwise a scarce top_k slot
    # gets wasted on a duplicate instead of a genuinely different table.
    tables = _apply_quarterly_fallback(tables)
    seen_tables: set = set()
    deduped_tables = []
    for t in tables:
        if t["table"] not in seen_tables:
            seen_tables.add(t["table"])
            deduped_tables.append(t)
    tables = deduped_tables

    # If the top qa_pairs.json hit is a STRONG match (95%+ on literal text OR
    # token-set/paraphrase similarity) to the user's raw query, GUARANTEE its
    # table is included, front of the list — stronger than the score-based
    # bonus above, which only makes the table likely to win fusion, not
    # certain. This is the "95%+ -> ground the LLM hard with this near-exact
    # example" tier. Token similarity catches real paraphrases (reordered
    # clauses, plurals, synonyms) that character-diff alone would miss.
    if qa_hits:
        _top_score, _top_qa = qa_hits[0]
        if best_qa_similarity(query, _top_qa["question"]) >= STRONG_MATCH_MIN_RATIO:
            strong_table = _top_qa["table"]
            tables = [t for t in tables if t["table"].upper() != strong_table.upper()]
            tables.insert(0, {"table": strong_table})
            if top_k:
                tables = tables[:top_k]

    table_names = {t["table"] for t in tables}

    # ── Columns: take top matches from selected tables ────────────────────────
    columns = [c for _, c in col_hits if c["table"] in table_names]
    # Deduplicate (table, column) pairs while preserving rank order
    seen_cols: set = set()
    unique_cols = []
    for c in columns:
        key = (c["table"], c["column"])
        if key not in seen_cols:
            seen_cols.add(key)
            unique_cols.append(c)
    columns = unique_cols[:TOP_K_COLUMNS * 2]

    # ── Row labels: restrict to selected tables ───────────────────────────────
    from src.description_fetcher import search_labels
    matched_labels = search_labels(query, table_names, top_k=TOP_K_LABELS)

    # ── Best qa_index match: surfaced separately for few-shot prompt injection ─
    # qa_hits is already sorted best-first (FAISS IndexFlatIP + normalised vecs).
    qa_example = None
    if qa_hits and qa_hits[0][0] >= QA_EXAMPLE_MIN_SCORE:
        score, qa = qa_hits[0]
        qa_example = {
            "question": qa["question"], "sql": qa["sql"], "table": qa["table"],
            "score": score,
            "text_similarity": text_similarity(query, qa["question"]),
            "token_similarity": token_similarity(query, qa["question"]),
        }

    return tables, columns, matched_labels, qa_example
