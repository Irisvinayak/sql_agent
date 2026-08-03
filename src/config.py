"""
All deployment/environment-specific values are read from environment variables,
loaded from a local `.env` file if one is present (see `.env.example` for the
full list and `MIGRATION_NOTES` at the bottom of this docstring).

Every setting keeps its EXACT previous default, so an existing deployment with
no `.env` file behaves identically to before — with one deliberate exception:
DB_HOST / DB_USER / DB_PASSWORD no longer have a hardcoded fallback (see the
"Oracle DB connection" section below for why).

Precedence: real environment variables (as set by the OS/shell/container) always
win over `.env` — `.env` only fills in values nothing else has already set.
"""

import os

from dotenv import load_dotenv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_REPO_ROOT, ".env"), override=False)


def _env_int(name: str, default: int) -> int:
    """int(os.environ[name]) with a safe fallback to `default` on missing/bad values."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[config] WARNING: {name}={raw!r} is not a valid int — using default {default}")
        return default


def _env_float(name: str, default: float) -> float:
    """float(os.environ[name]) with a safe fallback to `default` on missing/bad values."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[config] WARNING: {name}={raw!r} is not a valid float — using default {default}")
        return default


EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-large-en")

# Which folder retrieval reads its FAISS indexes / schema.json from.
# Defaults to the CIMS_RAQ(Quarterly) scoped build — that's what's actually
# under active development/testing (qa_pairs.json, direct-match tiers, the
# validation fixes). Production (embedding_building/output, no qa_index)
# used to be the default and repeatedly caused silent regressions whenever a
# server restart lost the EMBEDDING_DIR env var — every fix this session
# stopped applying with no error, just worse answers. Override with the
# EMBEDDING_DIR env var to point elsewhere, e.g.:
#   EMBEDDING_DIR=embedding_building/output python -m api.main
# Every module that needs this MUST read `config.EMBEDDING_DIR` at call time
# (not `from src.config import EMBEDDING_DIR`, which freezes a stale copy at
# import time and silently ignores any later reassignment or env override).
EMBEDDING_DIR = os.environ.get("EMBEDDING_DIR", "embedding_building/cims_raq_quarterly")

# Tied to EMBED_MODEL: this is BGE's required "asymmetric" query-side instruction
# prefix (documents are embedded with no prefix at all). Changing EMBED_MODEL to
# a non-BGE model without also changing/clearing this will silently degrade
# retrieval — the prefix text would just become noise prepended to every query.
QUERY_PREFIX = os.environ.get(
    "QUERY_PREFIX", "Represent this sentence for searching relevant passages: "
)

TOP_K_TABLES = _env_int("TOP_K_TABLES", 3)
TOP_K_COLUMNS = _env_int("TOP_K_COLUMNS", 5)

# How many candidate tables retrieval returns for the selector to choose from.
# This is a RECALL setting, not a precision one: the selector (src/selector.py)
# narrows it to one table before the SQL model sees anything. Do not feed this
# many tables to the SQL model directly — that is what caused it to JOIN every
# candidate and invent the foreign key (eval/results/hallucination_log.jsonl).
SHORTLIST_K = _env_int("SHORTLIST_K", 8)

# Token budget for the multi-part UNION ALL case, which is the longest SQL the
# model ever has to emit. Was 300, which truncated mid-identifier and produced
# fabricated table names (see hallucination_log.jsonl: "... FROM cims_raq").
MINIMAL_MULTIPART_NUM_PREDICT = _env_int("MINIMAL_MULTIPART_NUM_PREDICT", 1024)

# Ollama settings.
# Current effective default is the remote proxy (this was the active line before
# this refactor — the local-daemon line below it was commented out). Switch
# between them with the OLLAMA_URL env var / .env entry instead of editing this
# file:
#   local daemon : OLLAMA_URL=http://localhost:11434/api/generate
#   remote proxy : OLLAMA_URL=http://3.109.51.228/OllamaProxy/api/generate
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://3.109.51.228/OllamaProxy/api/generate")

# Set the active Ollama-served model here. Change only this value to swap models
# without modifying src/sql_generator.py.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "hf.co/defog/sqlcoder-7b-2:Q5_K_M")
# Context window sent to Ollama; the retry path resends schema + bad SQL + reason,
# so this needs headroom beyond the model's low default (often 2048) or retries return empty.
OLLAMA_NUM_CTX = _env_int("OLLAMA_NUM_CTX", 8192)

# How long Ollama keeps the model resident after a request. The default is 5
# minutes, so the first query after an idle spell pays a full model load (seconds
# for a 7B Q5), and alternating between the selector model and the SQL model can
# reload BOTH on every request if they do not fit in memory together. This is
# usually the single largest latency win available.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

# "compact" (default) or "json" for the selector's reply. JSON is measurably
# slower: constrained decoding costs per-token time, and the braces/keys/reasons
# roughly triple the output length for the same decision. Use "json" only if the
# selector model follows schemas better than it follows instructions.
SELECTOR_OUTPUT_FORMAT = os.environ.get("SELECTOR_OUTPUT_FORMAT", "compact")

# Kept as a plain in-code dict (not env-driven): it is keyed by exact Ollama
# model name, several keys have long inline rationale comments, and env vars
# cannot represent nested per-model structure cleanly. Only the ACTIVE model's
# temperature/num_predict are environment-overridable — see the block below.
MODEL_PROFILES = {
    "gpt-oss:120b-cloud": {
        "prompt_style": "rules",
        "dialect_hint": "Oracle",
        "temperature": 0.0,
        "num_predict": 512,
    },

    "qwen2.5:7b": {
        "prompt_style": "rules",
        "dialect_hint": "Oracle",
        "temperature": 0.0,
        "num_predict": 512,
    },
    "llama3.1:latest": {
        "prompt_style": "minimal",
        "dialect_hint": "Oracle",
        "temperature": 0.0,
        "num_predict": 128,
    },
    "hf.co/defog/sqlcoder-7b-2:Q5_K_M": {
        # "ddl": a real CREATE TABLE block with types, NOT NULL, PRIMARY KEY and
        # row-label values as column comments, in SQLCoder's own training prompt
        # format. The previous "minimal" style listed columns in prose and relied
        # on bullet-point instructions the model demonstrably ignored (it JOINed
        # every candidate table and invented the FK to do it).
        "prompt_style": "ddl",
        "dialect_hint": "Oracle",
        "temperature": 0.0,
        # 128 was starving generation: every one of the 36 records in
        # eval/results/hallucination_log.jsonl has first_attempt_sql == "" with
        # reason "Empty SQL", so the pipeline was always running on its retry
        # path. A vertical-table query with an exact label literal in the WHERE
        # clause alone exceeds 128 tokens.
        "num_predict": 512,
    },
    "hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q5_K_M": {
        "prompt_style": "minimal",
        "dialect_hint": "Oracle",
        "temperature": 0.0,
        "num_predict": 512,
    },
}

# Optional overrides for the two numeric generation knobs, applied ONLY to
# whichever profile OLLAMA_MODEL selects. Unset by default, so behavior is
# identical to before unless these are explicitly set — this exists so
# "generation configuration" (num_predict / temperature) can be tuned per
# deployment without editing MODEL_PROFILES.
if OLLAMA_MODEL in MODEL_PROFILES:
    if os.environ.get("OLLAMA_NUM_PREDICT", "").strip() != "":
        MODEL_PROFILES[OLLAMA_MODEL]["num_predict"] = _env_int(
            "OLLAMA_NUM_PREDICT", MODEL_PROFILES[OLLAMA_MODEL]["num_predict"]
        )
    if os.environ.get("OLLAMA_TEMPERATURE", "").strip() != "":
        MODEL_PROFILES[OLLAMA_MODEL]["temperature"] = _env_float(
            "OLLAMA_TEMPERATURE", MODEL_PROFILES[OLLAMA_MODEL]["temperature"]
        )

# Model used for the non-SQL reasoning calls — table/column selection
# (src/selector.py) and, when enabled, the reasoning plan. SQLCoder is a
# completion model: it cannot follow prose instructions or emit JSON, so it
# stays responsible only for writing SQL against a narrow typed schema block.
# This must be an INSTRUCT model, and it must be present in `ollama list`.
#
# qwen2.5-coder:7b (4.7GB) is the default because it follows the compact output
# format reliably. Note it loads alongside SQLCoder (4.8GB), so plan for ~9.5GB;
# if that thrashes, phi3:mini (2.2GB) is the low-memory alternative:
#   $env:SELECTOR_MODEL = "phi3:mini"
SELECTOR_MODEL = os.environ.get("SELECTOR_MODEL", "qwen2.5-coder:7b")

# ── Oracle DB connection ─────────────────────────────────────────────────────
# DB_HOST / DB_USER / DB_PASSWORD have NO hardcoded fallback — this repo
# previously committed a live Oracle password and bank-identifying username
# directly in source. They MUST come from a `.env` file (gitignored, never
# committed) or a real environment variable. If unset, these are empty strings
# and the Oracle connection will fail with a clear authentication/connect
# error — that is intentional; it is safer than a working-by-default hardcoded
# credential. See .env.example and MIGRATION_NOTES.md for setup.
DB_HOST = os.environ.get("DB_HOST", "")
DB_PORT = _env_int("DB_PORT", 1521)
DB_SERVICE = os.environ.get("DB_SERVICE", "XE")   # or SID, e.g. "xe" — not sensitive, safe default kept
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_MAX_ROWS = _env_int("DB_MAX_ROWS", 100)         # max rows to fetch per query
