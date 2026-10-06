#!/usr/bin/env python3
import argparse
import os
import re
import sys
from typing import Dict, List, Tuple

import psycopg

# --- DB config ---
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("ADMIN_USER")
DB_PASSWORD = os.getenv("ADMIN_PASSWORD")
DB_HOST = os.getenv("POSTGRES_HOST")
DB_PORT = os.getenv("POSTGRES_PORT")

if not all([DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT]):
    print(
        "ERROR: Set POSTGRES_DB, ADMIN_USER, ADMIN_PASSWORD, POSTGRES_HOST, and POSTGRES_PORT env vars.",
        file=sys.stderr,
    )
    sys.exit(1)
DB_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# TODO: handle references like "Articles 15 to 22 and 34" what else?


# --- Regex: match "Article <int>" and allow suffixes like "(a)" etc. by not enforcing the word boundary after digits
ARTICLE_REF_RE = re.compile(r"\bArticle\s+(\d+)", re.IGNORECASE)

# --- Regex: match "Article 3 to 5" or "Articles 3–5" (en-dash/em-dash/hyphen), or "through"
ARTICLE_RANGE_RE = re.compile(
    r"\bArticles?\s+(\d+)\s*(?:to|through|[-–—])\s*(\d+)",
    re.IGNORECASE,
)

# --- Regex: match "Article <n> and/or Article <m>" and "Articles <n> and/or <m>"
ARTICLE_AND_OR_RE = re.compile(
    r"\bArticles?\s+(\d+)\s*(?:,?\s*(?:and|or)\s+(\d+))",
    re.IGNORECASE,
)

# --- SQL ---
SELECT_ARTICLES_SQL = """
SELECT id, article_number, article_text
FROM articles
WHERE asset_id = %s
ORDER BY id
"""

INSERT_REL_SQL = """
INSERT INTO article_relations (
  from_article_id, to_article_id, relation_type
) VALUES (%s, %s, %s)
"""

RELATION_TYPE = "references"


def parse_args():
    ap = argparse.ArgumentParser(
        description="Scan articles for 'Article <n>' references (same document) and insert into article_relations."
    )
    ap.add_argument(
        "--asset-id",
        type=int,
        required=True,
        help="assets.id to process",
    )
    return ap.parse_args()


def should_include_reference(full_text: str, match_end: int) -> bool:
    """
    Apply rules to the text following 'Article <n>':

    1) If followed by 'of this' -> include (same document).
    2) Else if followed by 'of' -> exclude (external).
    3) Else include.
    """
    tail = full_text[match_end : match_end + 48].lstrip().lower()
    if tail.startswith("of this"):
        return True
    if tail.startswith("of"):
        return False
    return True


def load_articles(conn: psycopg.Connection, asset_id: int) -> List[Tuple[int, int, str]]:
    """
    Returns list of (article_id, article_number, article_text) for the given asset.
    """
    with conn.cursor() as cur:
        cur.execute(SELECT_ARTICLES_SQL, (asset_id,))
        rows = cur.fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def build_num_to_id(articles: List[Tuple[int, int, str]]) -> Dict[int, int]:
    """
    Build {article_number: article_id} for quick lookup.
    """
    m: Dict[int, int] = {}
    for art_id, art_no, _ in articles:
        m[art_no] = art_id
    return m


def extract_same_doc_refs(article_text: str, article_number: int) -> set[int]:
    """
    Return list of referenced article numbers in the same document,
    after applying the 3 rules.
    """
    refs: set[int] = set()

    for m in ARTICLE_RANGE_RE.finditer(article_text):
        try:
            start_n = int(m.group(1))
            end_n = int(m.group(2))
        except ValueError:
            continue

        if start_n > end_n:
            continue

        if should_include_reference(article_text, m.end()):
            for n in range(start_n, end_n + 1):
                refs.add(n)

    for m in ARTICLE_AND_OR_RE.finditer(article_text):
        try:
            first_n = int(m.group(1))
            second_n = int(m.group(2))
        except ValueError:
            continue

        print(f"Found and/or in Article {article_number}: Articles {first_n} and/or {second_n}")
        if should_include_reference(article_text, m.end()):
            print(f"  Including {first_n} and {second_n}")
            refs.add(first_n)
            refs.add(second_n)
        else:
            print(f"  Excluding {first_n} and {second_n}")

    for m in ARTICLE_REF_RE.finditer(article_text):
        num_str = m.group(1)
        try:
            num = int(num_str)
        except ValueError:
            continue
        if should_include_reference(article_text, m.end()):
            refs.add(num)

    return refs


def insert_relations(
    conn: psycopg.Connection,
    asset_id: int,
    relations: List[Tuple[int, int]],
) -> int:
    """
    Insert relations for a single asset.
    relations: list of (from_article_id, to_article_id)
    """
    if not relations:
        return 0
    with conn.cursor() as cur:
        for from_id, to_id in relations:
            cur.execute(
                INSERT_REL_SQL,
                (from_id, to_id, RELATION_TYPE),
            )
    conn.commit()
    return len(relations)


def main():
    args = parse_args()
    asset_id = args.asset_id

    try:
        with psycopg.connect(DB_URL) as conn:
            articles = load_articles(conn, asset_id)
            if not articles:
                print(f"No articles found for asset_id={asset_id}.", file=sys.stderr)
                sys.exit(1)

            num_to_id = build_num_to_id(articles)

            relations: List[Tuple[int, int]] = []

            for from_article_id, from_number, text in articles:
                if not text or not str(text).strip():
                    continue

                target_numbers = extract_same_doc_refs(text, from_number)
                if not target_numbers:
                    continue

                for to_number in target_numbers:
                    to_article_id = num_to_id.get(to_number)
                    if not to_article_id or to_article_id == from_article_id:
                        continue
                    key = (from_article_id, to_article_id)
                    relations.append(key)

            inserted = insert_relations(conn, asset_id, relations)
            print(f"Inserted {inserted} article_relations rows for asset_id={asset_id}.")

    except Exception as e:
        print(f"Failure: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
