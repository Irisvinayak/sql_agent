from sentence_transformers import SentenceTransformer
import numpy as np
import faiss
import os
import pickle
import re
from src.config import EMBED_MODEL, QUERY_PREFIX

model = SentenceTransformer(EMBED_MODEL)


def normalize_text(text):
    """Lowercase, collapse whitespace, strip trailing punctuation.

    Applied identically on both the index-build side (embed_documents) and
    the query side (embed_query) so the two are never compared asymmetrically.
    """
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[?.!,;:]+$", "", text)
    return text


def embed_documents(texts):
    texts = [normalize_text(t) for t in texts]
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=32,
        show_progress_bar=True,
    )
    return np.array(vectors, dtype="float32")


def embed_query(query):
    query = QUERY_PREFIX + normalize_text(query)
    return model.encode([query], normalize_embeddings=True)[0]


def build_faiss_index(vectors):

    vectors = np.array(vectors, dtype="float32")

    if len(vectors.shape) == 1:
        raise ValueError("Embedding output is 1D — check input texts")

    dim = vectors.shape[1]

    index = faiss.IndexFlatIP(dim)
    index.add(vectors)

    return index


def save_index(index, meta, index_path, meta_path):
    faiss.write_index(index, index_path)
    with open(meta_path, "wb") as f:
        pickle.dump(meta, f)


def load_existing_index(index_path, meta_path):
    """
    Load a previously-built FAISS index + its metadata records, if both files
    exist. Returns (records, vectors) with vectors[i] corresponding to
    records[i], or ([], None) if there is no prior build to reuse.

    Works for any IndexFlatIP built by build_faiss_index — that index type
    stores every vector verbatim, so reconstruct_n() recovers them exactly
    (no re-embedding needed to read them back).
    """
    if not (os.path.exists(index_path) and os.path.exists(meta_path)):
        return [], None
    index = faiss.read_index(index_path)
    with open(meta_path, "rb") as f:
        records = pickle.load(f)
    if index.ntotal != len(records):
        # A previous build was interrupted mid-write, or the two files are
        # from different runs — do not guess which records line up with
        # which vectors, just force a full re-embed instead.
        return [], None
    vectors = index.reconstruct_n(0, index.ntotal)
    return records, vectors


def embed_incremental(records, key_fn, text_fn, index_path, meta_path, force=False):
    """
    Build a FAISS index over `records`, reusing cached vectors from the index
    already on disk at index_path/meta_path for every record whose (key, text)
    pair is unchanged since that build — only genuinely new or edited records
    are actually run through the embedding model.

    This is what keeps onboarding a new return fast: without it, every build
    script re-embeds EVERY table/column/concept across every return already
    onboarded, every single time, even though almost none of that text ever
    changes between runs.

    key_fn(record) -> a stable identity for the record, e.g. a table name or
                       "table.column" string — used only to line up old and
                       new records, never embedded itself.
    text_fn(record) -> the exact text that would be embedded for this record.
                        If this differs from what was cached, the record is
                        re-embedded even if key_fn matches (so an edited
                        description is picked up correctly).

    Pass force=True to ignore any cache and re-embed everything — use this
    after changing EMBED_MODEL, or after a description-generation fix that
    could plausibly change text for many/all records at once.

    Returns (index, records) — records is returned unchanged (same object,
    same order) so callers can pass it straight to save_index().
    """
    old_records, old_vectors = ([], None) if force else load_existing_index(index_path, meta_path)
    cache = {}
    if old_vectors is not None:
        for rec, vec in zip(old_records, old_vectors):
            cache[(key_fn(rec), text_fn(rec))] = vec

    vectors = [None] * len(records)
    to_embed_positions = []
    to_embed_texts = []
    for i, rec in enumerate(records):
        cached = cache.get((key_fn(rec), text_fn(rec)))
        if cached is not None:
            vectors[i] = cached
        else:
            to_embed_positions.append(i)
            to_embed_texts.append(text_fn(rec))

    reused = len(records) - len(to_embed_texts)
    if to_embed_texts:
        new_vecs = embed_documents(to_embed_texts)
        for pos, vec in zip(to_embed_positions, new_vecs):
            vectors[pos] = vec

    tag = " (forced full re-embed)" if force else ""
    print(f"  [cache] {reused}/{len(records)} unchanged since the last build, reused as-is{tag}; "
          f"{len(to_embed_texts)} embedded fresh")

    index = build_faiss_index(np.array(vectors, dtype="float32"))
    return index, records


def build_row_label_index(samples: dict):
    """
    Build a FAISS index over every distinct row-label value fetched from the DB.

    Parameters
    ----------
    samples : dict
        Output of description_fetcher.fetch_and_save():
        { table_name: { col_name: [val1, val2, ...] } }

    Returns
    -------
    index  : faiss.Index  (IndexFlatIP, L2-normalised vectors)
    records: list of dicts with keys: table, column, value, text
    """
    records = []
    for table, col_map in samples.items():
        for col, values in col_map.items():
            for val in values:
                # Descriptive text so the embedding captures semantic meaning
                text = f"{table} {col} label: {val}"
                records.append({
                    "table": table,
                    "column": col,
                    "value": val,
                    "text": text,
                })

    if not records:
        return None, []

    texts = [r["text"] for r in records]
    vectors = embed_documents(texts)
    index = build_faiss_index(vectors)
    return index, records