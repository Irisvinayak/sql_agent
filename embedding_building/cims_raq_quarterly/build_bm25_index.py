"""
Builds a lexical (BM25) index over the SAME table documents the dense
table_index.faiss already embeds — Phase 1 of the retrieval accuracy roadmap.

Why this exists: dense embeddings smooth over exact structural markers.
CIMS_RAQ_Q_SEC1_PART_A_DOM and CIMS_RAQ_Q_SEC1_PART_B_DOM describe near-identical
column vocabulary, so their cosine scores differ by <0.02 — there is nothing in
a purely dense signal set built to catch an EXACT term like "Part B" deciding
between them. BM25 is exact-term-frequency scoring; it is not smarter than the
embedding, it is a different, complementary kind of match, which is why it is
fused as an ADDITIONAL signal in src/retriever.py rather than replacing anything.

Reuses the exact enriched table-document text `build_concept_embeddings.py`
already produces for the dense table_index (business-concept labels, dimension
axes, and all) — guarantees the lexical and dense corpora describe identically
the same tables, so fusing them is comparing apples to apples, not two
different descriptions of the same thing.

Run after build_concept_map.py and build_concept_embeddings.py (schema.json and
concept_map.json must both already exist):
    python embedding_building/cims_raq_quarterly/build_bm25_index.py
"""
import json
import os
import pickle
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(OUT_DIR, "schema.json")
CONCEPT_MAP_PATH = os.path.join(OUT_DIR, "concept_map.json")
OUT_PATH = os.path.join(OUT_DIR, "bm25_table_index.pkl")

from rank_bm25 import BM25Okapi

from embedding_building.cims_raq_quarterly.build_concept_embeddings import (
    enriched_table_documents,
)
from src.business_dictionary import expand_acronyms

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list:
    """
    Same abbreviation expansion the dense embedder's query side gets (via
    src/business_dictionary.py), applied here to the DOCUMENT side too, so
    "dom"/"npa"-style abbreviations in a query and their expansions in a
    document are comparable tokens either way. Plain lowercase word split
    otherwise — BM25 does its own term-frequency weighting, no stemming
    needed for a corpus this size.
    """
    return _TOKEN_RE.findall(expand_acronyms(text).lower())


def main():
    if not os.path.exists(SCHEMA_PATH):
        sys.exit(f"missing {SCHEMA_PATH} - run build_schema.py first")
    if not os.path.exists(CONCEPT_MAP_PATH):
        sys.exit(f"missing {CONCEPT_MAP_PATH} - run build_concept_map.py first")

    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    with open(CONCEPT_MAP_PATH, encoding="utf-8") as f:
        cm = json.load(f)

    records = enriched_table_documents(cm, schema)
    if not records:
        sys.exit("no table documents produced - nothing to index")

    corpus = [tokenize(r["text"]) for r in records]
    bm25 = BM25Okapi(corpus)

    with open(OUT_PATH, "wb") as f:
        pickle.dump({"bm25": bm25, "records": records}, f)

    print(f"indexed {len(records)} table documents -> {OUT_PATH}")
    print(f"sample tokens (first doc): {corpus[0][:20]}")


if __name__ == "__main__":
    main()
