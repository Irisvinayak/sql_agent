import json
import os

_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "table_qa.json")


def load_qa_pairs(path=None):
    """
    Flatten table_qa.json into one row per question:
      {table, question, gold_sql}
    table_qa.json is the ground-truth set: each table entry carries a list of
    qa_pairs (question -> hand-written correct SQL) used as the accuracy
    benchmark for retrieval + SQL generation.
    """
    path = path or _DEFAULT_PATH
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    for entry in data:
        table = entry.get("table_name")
        for qa in entry.get("qa_pairs", []):
            rows.append({
                "table": table,
                "question": qa["question"],
                "gold_sql": qa["sql"],
            })
    return rows
