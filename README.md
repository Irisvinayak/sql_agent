# CIMS SQL Agent — NL-to-SQL for RBI Regulatory Reporting

A natural-language-to-SQL system built for South Indian Bank's **CIMS** (Centralised Information Management System) regulatory reporting database. Type a question in plain English and get live Oracle query results — currently scoped to the **RAQ** (Risk Assessment Questionnaire) and **ALE** returns.

For how the pipeline actually works stage by stage, see **[PIPELINE.md](PIPELINE.md)**. For diagrams of the full system, see **[ARCHITECTURE.md](ARCHITECTURE.md)**. For adding a new return's embeddings, see **[EMBEDDING_GUIDE.md](EMBEDDING_GUIDE.md)**.

---

## Features

- **Natural language queries** — ask questions in plain English, get SQL + results
- **Hybrid semantic retrieval** — 7 fused signals (dense embeddings, BM25, XBRL concept matching, prior-question matching) find the right table from the CIMS schema
- **Banking domain awareness** — understands NPA, SMA, GNPA, SEC1/SEC2/SEC8, domestic/overseas splits, vertical-format tables, and RBI reporting conventions
- **Relative time resolution** — "last quarter", "this financial year" are resolved to exact date ranges before the LLM sees the query
- **SQL validation** — generated SQL is checked against the real schema before execution; hallucinated tables/columns and DML/DDL are hard-blocked
- **Exact-match fast path** — a question near-identical to a previously verified one replays the stored SQL directly, skipping the LLM entirely

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| Node.js | 18+ |
| Oracle Database | XE / any edition |
| [Ollama](https://ollama.ai) | latest, reachable at the URL in `.env` |

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd "sql query gen"
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

Copy `.env.example` to `.env` and fill in your Oracle credentials and Ollama endpoint:

```bash
cp .env.example .env
```

At minimum you must set `DB_HOST`, `DB_USER`, `DB_PASSWORD` (there is no hardcoded fallback for these on purpose). Every other setting has a working default — see `.env.example` for the full list with explanations.

### 4. Build the embedding indexes

The retrieval layer reads from `embedding_building/cims_raq_quarterly/` by default (`EMBEDDING_DIR` in `.env`). If you're onboarding a brand-new return, see **[EMBEDDING_GUIDE.md](EMBEDDING_GUIDE.md)** for the one-command pipeline. If the indexes already exist in that folder, you can skip this step.

### 5. Start the API server

```bash
python -m api.main
```

### 6. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

---

## Project structure

```
├── api/                    FastAPI application
│   ├── main.py             App setup, CORS, router registration
│   ├── schemas.py          Pydantic request/response models
│   ├── utils.py            Oracle row serialization helpers
│   └── routes/
│       ├── query.py        POST /api/query — the one production endpoint
│       └── health.py       GET  /api/health, GET /api/test-db
├── src/                    Core pipeline (serving-time, used by the API)
│   ├── config.py            All settings (env-driven, see .env.example)
│   ├── retriever.py          7-signal hybrid retrieval + RRF fusion
│   ├── selector.py            Deterministic table-shortlist narrowing (no LLM call)
│   ├── sql_generator.py        Prompt builder, Ollama call, correction-retry loop, validation
│   ├── executor.py             Oracle connection pool + execution
│   ├── vectorizer.py            Embedding + FAISS helpers
│   ├── business_dictionary.py   Acronym/alias/synonym expansion
│   ├── concept_map.py           XBRL business-concept reader
│   ├── business_semantics.py    Business-semantics prompt block
│   ├── literal_validator.py     Hallucinated-literal check
│   └── description_fetcher.py   Row-label sample fetching
├── embedding_building/      Everything related to building the vector store
│   └── cims_raq_quarterly/  The live embedding directory (schema.json + every index)
├── data/                    Oracle DDL, Excel-sourced descriptions, XBRL taxonomy exports
├── eval/                    Accuracy benchmarking harness
├── scripts/                 Offline tests, validators, one-off tooling
└── frontend/                React + Vite UI
```

---

## API

### POST /api/query

```json
// Request
{ "query": "Show total NPA for all banks in Q1 FY2024" }

// Response
{
  "query": "...",
  "matched_tables": ["cims_raq_q_sec1_part_a_dom"],
  "sql": "SELECT ...",
  "is_valid": true,
  "columns": ["PERIOD_DELINQUENCY", "TOTAL_LOAN_ASSETS"],
  "rows": [["C. Total ( A + B)", 123456.78]],
  "warnings": [],
  "timings_ms": { "retrieval": 42.1, "selection": 0.3, "llm_generation": 8210.5, "db_execution": 61.2 }
}
```

### GET /api/health

Liveness check.

### GET /api/test-db

Diagnostic-only Oracle connectivity check — not called by the frontend, intended for manual/curl use.

---

## Notes

- Queries shorter than 20 characters are rejected with a prompt to add more detail.
- The system detects missing time context and shows a soft hint to include a date/quarter/year.
- Relative time phrases ("last quarter", "this financial year") are resolved using India's April–March financial year convention.
- Backup tables (`_bk`, `_bkup`, `_backup` suffixes) are excluded from the search index automatically.
- There is no voice-input feature in the current codebase — earlier versions of this document described one; it has been fully removed from both backend and frontend.
