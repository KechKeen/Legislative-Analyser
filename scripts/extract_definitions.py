#!/usr/bin/env python3
import argparse
import os
import sys
import re
from lxml import etree
import psycopg
from typing import Dict

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


def normalize_text(s: str) -> str:
    return WS_MULTI.sub(" ", s.replace("\xa0", " ")).strip()


# TODO: some tags should be resolved instead of just removed (eg. quotation marks, maybe newlines)
def text_from(el: etree._Element) -> str:
    """Flatten element to plain text."""
    return " ".join(t.strip() for t in el.itertext() if t and t.strip())


def extract_definitions_map(xml_bytes: bytes) -> Dict[str, str]:
    root = etree.fromstring(xml_bytes)

    # 1. Find the <ARTICLE> with <STI.ART>Definitions</STI.ART>
    defs_article = None
    for art in root.findall(".//ARTICLE"):
        sti = art.find("./STI.ART")
        if sti is not None:
            full_text = " ".join(t.strip() for t in sti.itertext() if t.strip())
            if "definitions" in full_text.lower():
                defs_article = art
                break
    if defs_article is None:
        raise RuntimeError("No definitions article found.")

    # 2. Get top-level definition items only
    items = defs_article.findall("./ALINEA/LIST/ITEM")

    result = {}
    for item in items:
        # Remove the first top-level number label inside this item
        no_p = item.find("./NP/NO.P")
        if no_p is not None:
            no_p.getparent().remove(no_p)

        # Flatten the entire item to text (include nested lists etc.)
        raw_text = normalize_text(text_from(item))
        if not raw_text:
            continue

        # Split at first occurrence of "means"
        parts = re.split(r"\bmeans\b", raw_text, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            continue  # skip malformed ones

        term = parts[0]
        definition = raw_text

        if term:
            result[term] = definition

    return result


INSERT_SQL = """
INSERT INTO defined_terms (asset_id, term, definition)
VALUES (%s, %s, %s)
"""


def write_rows(conn: psycopg.Connection, asset_id: int, definitions: Dict[str, str]) -> int:
    if not definitions:
        return 0

    with conn.cursor() as cur:
        for term, definition in definitions.items():
            cur.execute(INSERT_SQL, (asset_id, term, definition))
    conn.commit()
    return len(definitions)


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


def parse_args():
    ap = argparse.ArgumentParser(
        description="POC: Parse FORMEX XML from assets.asset_text and extract+store definitions."
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

            definitions = extract_definitions_map(xml_bytes)
            if not definitions:
                print("No definitions found.", file=sys.stderr)
                sys.exit(1)

            written = write_rows(conn, args.asset_id, definitions)
            print(f"Inserted/updated {written} defined terms for asset_id={args.asset_id}.")

    except Exception as e:
        print(f"Failure: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
