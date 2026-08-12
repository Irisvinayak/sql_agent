"""
Offline consistency check for embedding_building/business_dictionary.yaml — no
DB, no LLM. Run after every edit to the dictionary, same discipline as
scripts/test_accuracy_guards.py for the rest of the schema.

Checks:
  1. Every alias `canonical` value is a real, live table name in schema.json.
     A stale alias degrades silently at request time (detect_pinned_table
     just returns a name nothing resolves to) — this check is what turns that
     into a loud, pre-merge failure instead.
  2. No alias surface form collides with a DIFFERENT real table's name. This
     is the exact bug class the "part b"/"part c" collision comment in the
     yaml file describes: an unqualified alias would silently steal traffic
     from a genuinely-matching table.
  3. No surface form is registered twice across acronyms/synonyms/aliases
     with different canonical targets (an ambiguous entry should be explicit,
     not silently last-write-wins).

Usage:
    python -m scripts.validate_business_dictionary
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml

from src import config

DICT_PATH = os.path.join(ROOT, "embedding_building", "business_dictionary.yaml")


def _schema_tables() -> set:
    path = os.path.join(config.EMBEDDING_DIR, "schema.json")
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    return {t["table"].lower() for t in schema if not t.get("is_backup")}


def main():
    with open(DICT_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    tables = _schema_tables()
    failures = []

    # 1. every alias target must be a real, live table
    aliases = raw.get("aliases") or []
    for entry in aliases:
        target = entry["canonical"].lower()
        if target not in tables:
            failures.append(
                f"alias {entry['forms']!r} points to {target!r}, which is not "
                f"in schema.json (stale entry, or table renamed/removed)"
            )

    # 2. no alias surface form collides with a DIFFERENT real table
    for entry in aliases:
        target = entry["canonical"].lower()
        for form in entry["forms"]:
            pattern = re.compile(rf"\b{re.escape(form)}\b", re.IGNORECASE)
            for real_table in tables:
                if real_table == target:
                    continue
                # A collision is when the alias form appears as a substring of
                # another real table's own natural-language reading — checked
                # here against the table name's tokens, which is the same
                # surface a user would plausibly type.
                readable = real_table.replace("_", " ")
                if pattern.search(readable):
                    failures.append(
                        f"alias form {form!r} (-> {target}) also matches real "
                        f"table {real_table!r} - this alias would steal that "
                        f"table's traffic. Qualify the form or narrow it."
                    )

    # 3. no surface form registered twice with different canonical targets
    seen: dict[str, str] = {}
    for category in ("acronyms", "synonyms", "aliases"):
        for entry in raw.get(category) or []:
            canonical = entry["canonical"]
            for form in entry["forms"]:
                key = form.lower()
                if key in seen and seen[key] != canonical:
                    failures.append(
                        f"surface form {form!r} is registered twice with "
                        f"different targets: {seen[key]!r} vs {canonical!r}"
                    )
                seen[key] = canonical

    print(f"acronyms={len(raw.get('acronyms') or [])}  "
          f"synonyms={len(raw.get('synonyms') or [])}  "
          f"aliases={len(aliases)}")

    if failures:
        print(f"\n{len(failures)} FAILED\n")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("All checks passed.")


if __name__ == "__main__":
    main()
