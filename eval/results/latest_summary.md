# Accuracy Report — 20260805_152313

`CONTEXT_PIPELINE=slice` · `QA_SIGNAL_WEIGHT=0.0` · `embedding_building/cims_raq_quarterly` · `hf.co/defog/sqlcoder-7b-2:Q5_K_M`

## Overall
- Questions evaluated: **2**
- Table retrieval accuracy: **100.0%**
- Avg. column recall: **41.7%**
- SQL validity rate: **0.0%**
- Execution success rate (of valid SQL run against DB): **n/a**
- Execution match accuracy (rows == gold SQL's rows): **n/a**

## Per-table breakdown

| Table | N | Table Hit % | SQL Valid % | Exec Match % |
|---|---|---|---|---|
| CIMS_RAQ_Q_SEC1_PART_A_DOM | 1 | 100.0% | 0.0% | n/a |
| CIMS_RAQ_Q_SEC2_PART_A | 1 | 100.0% | 0.0% | n/a |
