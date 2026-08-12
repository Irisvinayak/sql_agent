# Architecture Diagrams

Natural language → SQL pipeline for RBI CIMS regulatory returns (RAQ, ALE), Oracle backend. Diagrams are Mermaid; they render on GitHub and in most Markdown viewers.

**Note on table selection:** `src/selector.py` is fully deterministic today and makes no LLM call — an earlier LLM-based version (`qwen2.5-coder:7b` via the Ollama proxy) was tried and removed because a single call cost 75–135s+. The diagrams below reflect the current deterministic implementation, not the earlier design.

---

## 1. Component architecture

```mermaid
graph TB
    subgraph CLIENT["Client — React 18 + Vite 5 + Tailwind 3"]
        UI["Home page<br/><i>frontend/src/pages/Home</i>"]
        COMP["QueryInput · SqlDisplay<br/>ResultsTable · StatusBadge · TimingsPanel"]
        APIJS["api.js<br/><i>fetch wrapper</i>"]
        UI --> COMP --> APIJS
    end

    subgraph API["API — FastAPI + Uvicorn (CORS :5173)"]
        MAIN["main.py<br/><i>startup, router registration</i>"]
        QROUTE["routes/query.py<br/><b>POST /query</b> — orchestrator"]
        HROUTE["routes/health.py<br/>GET /health, GET /test-db"]
        SCHEMAS["schemas.py<br/><i>QueryRequest / QueryResult</i>"]
        MAIN --> QROUTE
        MAIN --> HROUTE
        QROUTE -.validates via.-> SCHEMAS
    end

    subgraph CORE["Pipeline core — src/"]
        RETR["retriever.py<br/><i>7-signal RRF fusion + hybrid blend<br/>+ precedence tiers</i>"]
        SEL["selector.py<br/><i>deterministic shortlist → 1 table<br/>NO network call</i>"]
        GEN["sql_generator.py<br/><i>prompt build · correction loop · validate</i>"]
        EXEC["executor.py<br/><i>oracledb pool · dry run · execute</i>"]
        VEC["vectorizer.py<br/><i>SentenceTransformer wrapper</i>"]
        CM["concept_map.py<br/><i>XBRL business layer +<br/>check_stock_aggregation()</i>"]
        LV["literal_validator.py<br/><i>check_literal_validity()</i>"]
        BS["business_semantics.py<br/><i>prompt block renderer</i>"]
        SEML["semantic_layer.py<br/><i>join allow-list</i>"]
        SECA["section_alias.py<br/><i>'Section 12' → table</i>"]
        BD["business_dictionary.py<br/><i>acronyms · synonyms · alias pins</i>"]
        LEX["lexical_search.py<br/><i>BM25 signal</i>"]
        DESC["description_fetcher.py<br/><i>row-label samples</i>"]
        CFG["config.py<br/><i>all env-driven settings</i>"]
    end

    subgraph MODELS["Models"]
        EMB["<b>bge-large-en</b> · 1024-d<br/>sentence-transformers<br/><i>LOCAL, in-process</i>"]
        SQLM["<b>sqlcoder-7b-2</b> Q5_K_M<br/><i>REMOTE via Ollama — the only LLM call</i>"]
    end

    subgraph STORE["Stores"]
        FAISS["FAISS IndexFlatIP · faiss-cpu<br/>table · column · row_label<br/>qa · concept<br/><i>+ .pkl payload sidecars</i>"]
        ARTI["schema.json · concept_map.json<br/>semantic_layer.yaml<br/>qa_pairs.json · description_samples.json"]
        ORA[("Oracle<br/><i>RAQ + ALE tables</i>")]
    end

    subgraph PROXY["Ollama proxy"]
        OP["/OllamaProxy/api/generate"]
    end

    APIJS ==>|"HTTP JSON"| QROUTE

    QROUTE ==> RETR
    QROUTE ==> SEL
    QROUTE ==> GEN
    QROUTE ==> EXEC

    RETR --> VEC
    RETR --> SECA
    RETR --> DESC
    RETR --> BD
    RETR --> LEX
    SEL --> SEML
    GEN --> BS
    BS --> CM
    GEN --> SEML
    GEN --> CM
    GEN --> LV
    LV --> DESC

    VEC ==> EMB
    GEN ==>|"HTTP, only network call in the request path"| OP
    OP ==> SQLM

    RETR --> FAISS
    CM --> ARTI
    GEN --> ARTI
    SEML --> ARTI
    BD --> BDY["business_dictionary.yaml"]
    LEX --> BM25I["bm25_table_index.pkl"]
    EXEC ==>|"python-oracledb<br/>thin mode, pooled"| ORA

    QROUTE -.->|"result.warnings, timings_ms"| SCHEMAS

    CFG -.->|"read at call time"| RETR
    CFG -.-> SEL
    CFG -.-> GEN
    CFG -.-> EXEC

    classDef client fill:#e8f0f6,stroke:#1d5f8a,color:#12202b
    classDef api fill:#e4f1ec,stroke:#1f7a5c,color:#0f2b22
    classDef core fill:#f4f1e8,stroke:#8a6d1d,color:#2b2412
    classDef model fill:#f8e8e6,stroke:#a4342b,color:#2b1210
    classDef store fill:#eceef1,stroke:#5a6472,color:#1a1f26
    classDef proxy fill:#f6ecdd,stroke:#9c5c0c,color:#2b1f0c

    class UI,COMP,APIJS client
    class MAIN,QROUTE,HROUTE,SCHEMAS api
    class RETR,SEL,GEN,EXEC,VEC,CM,LV,BS,SEML,SECA,BD,LEX,DESC,CFG core
    class EMB,SQLM model
    class FAISS,ARTI,ORA,BDY,BM25I store
    class OP proxy
```

---

## 2. Model touchpoints

Exactly one remote LLM call exists in the request path (plus its correction retries) — table selection is deterministic and makes no model call at all.

```mermaid
graph LR
    Q["User question"]

    subgraph LOCAL["Runs in the API process"]
        E["<b>bge-large-en</b><br/>SentenceTransformer<br/>1024-d, L2-normalised<br/>~100ms CPU"]
    end

    subgraph DETERM["Deterministic — no model, no network call"]
        SELD["selector.py<br/>strong-match / dominance-ratio /<br/>declared-join / top-1 fallback"]
    end

    subgraph REMOTE["Remote — one model"]
        G["<b>sqlcoder-7b-2:Q5_K_M</b><br/><i>completion</i> — writes SQL only<br/>temp 0 · up to 3 correction retries"]
    end

    Q -->|"business_dictionary.expand_acronyms()<br/>npa → non performing assets"| E
    E -->|"ONE vector,<br/>reused across every signal"| F["7 signals (5 FAISS + concept + BM25)<br/>= RRF fusion + hybrid blend (gamma 0.3)"]
    F -->|"shortlist"| SELD
    SELD -->|"1 table (or 2 via declared join)"| G
    G -->|"SQL"| V["validate + Oracle dry run<br/>+ stock/flow + literal-validity checks"]
    V -->|"invalid, ≤3 rounds"| G

    classDef local fill:#e4f1ec,stroke:#1f7a5c,color:#0f2b22
    classDef determ fill:#eceef1,stroke:#5a6472,color:#1a1f26
    classDef remote fill:#f8e8e6,stroke:#a4342b,color:#2b1210
    classDef flow fill:#eceef1,stroke:#5a6472,color:#1a1f26

    class E local
    class SELD determ
    class G remote
    class Q,F,V flow
```

---

## 3. How a question becomes an answer

Non-technical overview. Every question passes safeguards before any data is read, and questions we have answered before skip the AI entirely.

```mermaid
flowchart TD
    Q(["Analyst asks a question<br/>in plain English"])

    Q --> CLEAR{"Enough detail<br/>to answer?"}
    CLEAR -->|"too vague"| ASK(["Ask for the metric<br/>and the period"])

    CLEAR -->|yes| KNOWN{"Asked before?"}
    KNOWN -->|"yes — recognised"| REUSE(["<b>Reuse the approved answer</b><br/>Written and checked by hand.<br/>No AI involved, instant."])

    KNOWN -->|"no — new question"| FIND["<b>Find the right part of the return</b><br/>Searches the regulatory vocabulary,<br/>not just column names"]

    FIND --> PICK["<b>Narrow to one section</b><br/>A deterministic rule confirms the single<br/>correct table before anything is written"]

    PICK --> DRAFT["<b>Draft the query</b><br/>Given only that one section's structure,<br/>its real row labels, and the reporting unit"]

    DRAFT --> SAFE{"<b>Safety checks</b><br/>Read-only · real columns only<br/>test-run against the database"}
    SAFE -->|"problem found"| RETRY["Correct and re-check<br/><i>up to 3 attempts</i>"]
    RETRY --> DRAFT

    SAFE -->|passes| RUN["<b>Run the query</b><br/>Read-only, capped at 100 rows"]
    RUN --> OUT(["<b>Answer</b><br/>figures · the query used<br/>· any caveats"])

    SAFE -->|"still failing"| FLAG(["Return the problem,<br/>not a guess"])

    classDef ask fill:#e4f1ec,stroke:#1f7a5c,stroke-width:2px,color:#0f2b22
    classDef work fill:#e8f0f6,stroke:#1d5f8a,color:#12202b
    classDef check fill:#f6ecdd,stroke:#9c5c0c,color:#2b1f0c
    classDef done fill:#e4f1ec,stroke:#1f7a5c,stroke-width:2px,color:#0f2b22

    class Q,ASK,FLAG ask
    class FIND,PICK,DRAFT,RUN,RETRY work
    class CLEAR,KNOWN,SAFE check
    class REUSE,OUT done
```

**What this buys us**

| | |
|---|---|
| **Repeat questions are free** | A recognised question replays an answer a human already verified — no AI, no cost, no risk of a different answer next time. |
| **The AI is never given a free hand** | It sees one section of the return, that section's real row labels, and nothing else. |
| **Nothing is written, ever** | Read-only by design, enforced before execution, not by convention. |
| **Wrong beats invented** | If the query cannot be made valid in three attempts, the system reports the problem instead of returning a plausible number. |
| **Every answer is auditable** | The query used is returned alongside the figures, so any number can be traced back to the exact rows it came from. |

---

## 4. Same flow, engineering detail

```mermaid
flowchart TD
    START(["POST /query"]) --> G0{"len ≥ 20 chars?"}
    G0 -->|no| X0(["return needs_more_info"])
    G0 -->|yes| G1{"time expression<br/>present?"}
    G1 -->|no| HINT["attach accuracy_hint"]
    G1 -->|yes| EMB1
    HINT --> EMB1

    EMB1["<b>1 · Embed once</b><br/>business_dictionary.expand_acronyms → bge-large-en<br/>one 1024-d vector, reused everywhere"]

    EMB1 --> QA{"<b>2 ·</b> literal similarity<br/>to a stored question<br/>≥ 0.99?"}
    QA -->|yes| X1(["replay verified SQL<br/>validate → execute<br/>source=direct_match"])

    QA -->|no| RET["<b>3 · Retrieval — widen (k=8)</b><br/>qa 2.5 · table 2.0 · concept 2.0<br/>column 1.5 · bm25 1.5 · row_label 1.0<br/>fused by RRF + hybrid blend (gamma 0.3)"]

    RET --> TIER["precedence tiers, in order:<br/>relative-floor prune (15%,<br/>floor computed pre-QA-bonus)<br/>→ 95% QA match forced to front<br/>→ business-dictionary alias pin<br/>→ explicit section pinned"]

    TIER --> EMPTY{"any table?"}
    EMPTY -->|no| X2(["no matching tables"])

    EMPTY -->|yes| SELQ{"<b>4 · selector.py</b> (deterministic, no LLM):<br/>strong match? -> skip<br/>&lt;2 candidates? -> skip<br/>top ≥2x runner-up? -> skip<br/>else: declared-join pair, or top-1"}

    SELQ --> PRUNE["<b>5 · Prune</b><br/>drop columns + row labels<br/>of rejected tables"]

    PRUNE --> GEN["<b>6 · Generate</b> — sqlcoder-7b-2<br/>Task → DDL → business semantics<br/>→ time → example → rules → Answer"]

    GEN --> CHK{"validate_sql<br/>AND Oracle dry run"}
    CHK -->|"invalid,<br/>< 3 rounds"| FIX["deterministic autocorrect, else re-prompt<br/>+ failed-attempt memory + same-SQL early exit<br/>+ temp bump on final round only"]
    FIX --> GEN

    CHK -->|valid| VAL["<b>7 · Validate</b><br/>SELECT only · no DML/DDL<br/>TO_DATE-wrapped literals<br/>declared joins only<br/>+ check_stock_aggregation()<br/>(gated by BUSINESS_SEMANTICS_LEVEL)<br/>+ check_literal_validity() (always on)"]

    VAL --> EX["<b>8 · Execute</b><br/>oracledb pool · max 100 rows"]
    EX --> RESP(["<b>9 · Respond</b><br/>sql · rows · warnings<br/>source · timings_ms"])

    CHK -->|"invalid after 3"| RESP

    classDef exit fill:#e4f1ec,stroke:#1f7a5c,color:#0f2b22
    classDef stage fill:#e8f0f6,stroke:#1d5f8a,color:#12202b
    classDef model fill:#f8e8e6,stroke:#a4342b,color:#2b1210
    classDef bad fill:#f6ecdd,stroke:#9c5c0c,color:#2b1f0c
    classDef gate fill:#eceef1,stroke:#5a6472,color:#1a1f26

    class X0,X1,X2,RESP exit
    class EMB1,RET,TIER,PRUNE,VAL,EX,HINT stage
    class GEN model
    class FIX bad
    class G0,G1,QA,EMPTY,SELQ,CHK gate
```

---

## 5. Offline build vs runtime read

Nothing in the request path writes. Every artifact is built ahead of time; the API only reads. See **[EMBEDDING_GUIDE.md](EMBEDDING_GUIDE.md)** for the full build pipeline.

```mermaid
graph TB
    subgraph SRC["Sources of truth"]
        XSD["XBRL taxonomy exports<br/><i>data/&lt;return&gt;.json</i>"]
        DDL["data/schema.sql<br/><i>Oracle DDL dump</i>"]
        DESCX["data/.json-formatted<br/><i>column descriptions</i>"]
        LIVE[("Oracle<br/><i>live row labels</i>")]
    end

    subgraph BUILD["Build — embedding_building/cims_raq_quarterly/"]
        BSCH["build_schema.py"]
        BCM["build_concept_map.py"]
        BEMB["build_embeddings.py"]
        BCE["build_concept_embeddings.py"]
        BQA["build_qa_index.py"]
        BBM["build_bm25_index.py<br/><i>reuses BCE's table documents</i>"]
        BNEW["build_new_return.py<br/><i>orchestrates the above, in order</i>"]
    end

    subgraph ART["Artifacts read at runtime"]
        SJ["schema.json"]
        CMJ["concept_map.json"]
        IDX["table · column · row_label<br/>concept · qa indexes"]
        BM25A["bm25_table_index.pkl"]
        DS["description_samples.json"]
        SL["semantic_layer.yaml"]
        BDY["business_dictionary.yaml<br/><i>acronyms · synonyms · aliases<br/>hand-maintained, not generated</i>"]
    end

    DDL --> BSCH
    DESCX --> BSCH
    LIVE --> BSCH
    BSCH --> SJ

    XSD --> BCM
    BCM --> CMJ

    SJ --> BEMB
    LIVE --> BEMB
    BEMB --> IDX
    BEMB --> DS

    CMJ --> BCE
    SJ --> BCE
    BCE --> IDX

    BQA --> IDX
    SJ --> BBM
    BBM --> BM25A

    BNEW -.orchestrates.-> BSCH
    BNEW -.orchestrates.-> BEMB
    BNEW -.orchestrates.-> BCM
    BNEW -.orchestrates.-> BCE
    BNEW -.orchestrates.-> BBM

    classDef src fill:#eceef1,stroke:#5a6472,color:#1a1f26
    classDef build fill:#f4f1e8,stroke:#8a6d1d,color:#2b2412
    classDef art fill:#e8f0f6,stroke:#1d5f8a,color:#12202b

    class XSD,DDL,DESCX,LIVE src
    class BSCH,BCM,BEMB,BCE,BQA,BBM,BNEW build
    class SJ,CMJ,IDX,BM25A,DS,SL,BDY art
```

The API caches indexes and artifacts for the life of the process — a rebuild alone will not be picked up; the API process must be restarted.
