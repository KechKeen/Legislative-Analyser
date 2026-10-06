#!/usr/bin/env python3
import argparse
import os
import sys
import re
from lxml import etree
import psycopg
from typing import List, Dict

# --- DB setup ---
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

WS_MULTI = re.compile(r"\s+")

# --- Helpers ---


def normalize_text(s: str) -> str:
    return WS_MULTI.sub(" ", s.replace("\xa0", " ")).strip()


def text_from(el: etree._Element) -> str:
    """Flatten element to plain text."""
    return " ".join(t.strip() for t in el.itertext() if t and t.strip())


def get_article_number(el: etree._Element) -> int:
    """Try to extract the numeric part of the IDENTIFIER attribute, e.g. '004' -> 4."""
    ident = el.get("IDENTIFIER")
    if not ident:
        return -1
    tail = ident.split(".")[-1]
    return int(tail) if tail.isdigit() else -1


# --- Extraction logic ---


def extract_articles_map(xml_bytes: bytes) -> List[Dict[str, str]]:
    root = etree.fromstring(xml_bytes)

    articles = []
    for art in root.findall(".//ARTICLE"):
        article_number = get_article_number(art)
        raw_text = normalize_text(text_from(art))
        if not raw_text:
            continue

        articles.append(
            {
                "article_number": article_number,
                "article_text": raw_text,
            }
        )

    return articles


# --- DB writing ---

INSERT_SQL = """
INSERT INTO articles (asset_id, article_number, article_text)
VALUES (%s, %s, %s)
ON CONFLICT ON CONSTRAINT uq_article
DO UPDATE SET
  article_text = EXCLUDED.article_text
RETURNING id;
"""


def write_rows(conn: psycopg.Connection, asset_id: int, articles: List[Dict[str, str]]) -> int:
    if not articles:
        return 0

    with conn.cursor() as cur:
        for art in articles:
            cur.execute(
                INSERT_SQL,
                (asset_id, art["article_number"], art["article_text"]),
            )
    conn.commit()
    return len(articles)


ASSET_SELECT_SQL = """
SELECT asset_text
FROM assets
WHERE id = %s
"""


def fetch_asset_xml(conn: psycopg.Connection, asset_id: int) -> bytes:
    with conn.cursor() as cur:
        cur.execute(ASSET_SELECT_SQL, (asset_id,))
        row = cur.fetchone()

    if not row:
        raise RuntimeError(f"Asset id={asset_id} not found.")

    asset_text = row[0]
    if not asset_text or not str(asset_text).strip():
        raise RuntimeError(f"Asset id={asset_id} has empty asset_text.")

    if isinstance(asset_text, bytes):
        return asset_text
    return str(asset_text).encode("utf-8", errors="replace")


# --- CLI ---


def parse_args():
    ap = argparse.ArgumentParser(
        description="POC: Extract full text of each <ARTICLE> and insert into 'articles' table."
    )
    ap.add_argument(
        "--asset-id",
        type=int,
        required=True,
        help="assets.id to process",
    )
    return ap.parse_args()


def main():
    args = parse_args()

    try:
        with psycopg.connect(DB_URL) as conn:
            xml_bytes = fetch_asset_xml(conn, args.asset_id)
            articles = extract_articles_map(xml_bytes)

            if not articles:
                print("No articles found.", file=sys.stderr)
                sys.exit(1)

            written = write_rows(conn, args.asset_id, articles)
            print(f"Inserted {written} articles for asset_id={args.asset_id}.")

    except Exception as e:
        print(f"Failure: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
