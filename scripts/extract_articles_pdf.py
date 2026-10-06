#!/usr/bin/env python3
# TODO: DELETEME
import argparse
import os
import re
import sys
from typing import List, Dict, Any
import psycopg

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
ARTICLE_RE = re.compile(r"^Article\s+(\d+)\b", re.IGNORECASE)
CHAPTER_RE = re.compile(r"^Chapter\s+([IVXLC]+|\d+)\b", re.IGNORECASE)
SECTION_RE = re.compile(r"^Section\s+(\d+)\b", re.IGNORECASE)
END_RE = re.compile(
    r"^This\s+Regulation\s+shall\s+be\s+binding\s+in\s+its\s+entirety", re.IGNORECASE
)

INSERT_SQL = """
INSERT INTO articles (asset_id, article_number, article_title, article_text)
VALUES (%s, %s, %s, %s)
"""

ASSET_SELECT_SQL = """
SELECT asset_text
FROM assets
WHERE id = %s
"""


def normalize_line(s: str) -> str:
    if s is None:
        return ""
    s = s.replace("\xa0", " ")
    s = s.strip()
    s = WS_MULTI.sub(" ", s)
    return s


def extract_articles_from_pdf_text(text: str) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    articles: List[Dict[str, Any]] = []
    current_article: Dict[str, Any] | None = None

    def flush_current():
        nonlocal current_article
        if current_article is not None and "article_text" not in current_article:
            current_article["article_text"] = "\n".join(current_article["lines"]).strip()
            articles.append(current_article)
        current_article = None

    for raw_line in lines:
        line = normalize_line(raw_line)

        if not line:
            if current_article is not None:
                current_article["lines"].append("")
            continue

        if END_RE.match(line):
            flush_current()
            break

        if CHAPTER_RE.match(line) or SECTION_RE.match(line):
            flush_current()
            continue

        m_art = ARTICLE_RE.match(line)
        if m_art:
            flush_current()
            art_num = int(m_art.group(1))
            current_article = {
                "article_number": art_num,
                "article_title": None,
                "lines": [line],
            }
            continue

        if current_article is None:
            continue

        if current_article["article_title"] is None:
            current_article["article_title"] = line
            current_article["lines"].append(line)
        else:
            current_article["lines"].append(line)

    flush_current()
    articles = [a for a in articles if a.get("article_text")]
    return articles


def fetch_asset_text(conn: psycopg.Connection, asset_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute(ASSET_SELECT_SQL, (asset_id,))
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Asset id={asset_id} not found.")
    asset_text = row[0]
    if not asset_text or not str(asset_text).strip():
        raise RuntimeError(f"Asset id={asset_id} has empty asset_text.")
    return str(asset_text)


def write_articles(conn: psycopg.Connection, asset_id: int, articles: List[Dict[str, Any]]) -> int:
    if not articles:
        return 0
    with conn.cursor() as cur:
        for art in articles:
            cur.execute(
                INSERT_SQL,
                (
                    asset_id,
                    art["article_number"],
                    art.get("article_title") or None,
                    art["article_text"],
                ),
            )
    conn.commit()
    return len(articles)


def parse_args():
    ap = argparse.ArgumentParser(
        description="Extract EUR-Lex-style articles from PDF text stored in assets.asset_text and insert into articles."
    )
    ap.add_argument("--asset-id", type=int, required=True)
    return ap.parse_args()


def main():
    args = parse_args()
    try:
        with psycopg.connect(DB_URL) as conn:
            pdf_text = fetch_asset_text(conn, args.asset_id)
            articles = extract_articles_from_pdf_text(pdf_text)
            if not articles:
                print("No articles detected in text.", file=sys.stderr)
                sys.exit(1)
            inserted = write_articles(conn, args.asset_id, articles)
            print(f"Inserted {inserted} article rows for asset_id={args.asset_id}.")
    except Exception as e:
        print(f"Failure: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
