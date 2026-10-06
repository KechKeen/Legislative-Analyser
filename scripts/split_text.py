#!/usr/bin/env python3
import argparse
import os
import re
import sys
from typing import List, Optional

import psycopg
from lxml import etree

# TODO: add document/section titles, use them when embedding

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


def text_from(el: etree._Element) -> str:
    return " ".join(t.strip() for t in el.itertext() if t and t.strip())


def get_identifier_tail_number(el: etree._Element) -> Optional[int]:
    ident = el.get("IDENTIFIER")
    if not ident:
        return None
    tail = ident.split(".")[-1]
    return int(tail) if tail.isdigit() else None


def parse_xml_rows(xml_bytes: bytes) -> List[dict]:
    root = etree.fromstring(xml_bytes)
    rows: List[dict] = []

    for art in root.findall(".//ENACTING.TERMS//ARTICLE"):
        art_no = get_identifier_tail_number(art)
        for item in art.findall("./*"):
            if item.tag not in ("PARAG", "ALINEA"):
                continue

            text_normalized = text_from(item)
            if not text_normalized or not text_normalized.strip():
                continue

            rows.append(
                {
                    "article_number": art_no,
                    "part_number": get_identifier_tail_number(item),
                    "part_tag": item.tag,
                    "part_text_raw": etree.tostring(item, encoding="unicode"),
                    "part_text": normalize_text(text_from(item)),
                }
            )

    return rows


UPSERT_SQL = """
INSERT INTO asset_parts (
    asset_id,
    part_index,
    article_number,
    part_number,
    part_tag,
    part_text_raw,
    part_text,
    language_code
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
"""


def write_rows(
    conn: psycopg.Connection,
    asset_id: int,
    rows: List[dict],
    language_code: str,
    batch_size: int = 1000,
) -> int:
    total = 0
    with conn.cursor() as cur:
        for off in range(0, len(rows), batch_size):
            batch = rows[off : off + batch_size]
            values = [
                (
                    asset_id,
                    i,
                    r["article_number"],
                    r["part_number"],
                    r["part_tag"],
                    r["part_text_raw"],
                    r["part_text"],
                    language_code,
                )
                for i, r in enumerate(batch, start=off)
            ]
            cur.executemany(UPSERT_SQL, values)
            total += len(values)
    conn.commit()
    return total


ASSET_SELECT_SQL = """
SELECT asset_text, language_code
FROM assets
WHERE id = %s
"""


def fetch_asset_xml_and_lang(conn: psycopg.Connection, asset_id: int) -> tuple[bytes, str]:
    with conn.cursor() as cur:
        cur.execute(ASSET_SELECT_SQL, (asset_id,))
        row = cur.fetchone()

    if not row:
        raise RuntimeError(f"Asset id={asset_id} not found.")

    asset_text, lang = row
    if not asset_text or not str(asset_text).strip():
        raise RuntimeError(f"Asset id={asset_id} has empty asset_text.")

    # Ensure bytes for XML parser
    if isinstance(asset_text, bytes):
        xml_bytes = asset_text
    else:
        xml_bytes = str(asset_text).encode("utf-8", errors="replace")

    return xml_bytes, lang


def parse_args():
    ap = argparse.ArgumentParser(
        description="POC: Parse FORMEX XML from assets.asset_text and store parts."
    )
    ap.add_argument(
        "--asset-id",
        type=int,
        required=True,
        help="assets.id to read + attach parts to",
    )
    return ap.parse_args()


def main():
    args = parse_args()

    try:
        with psycopg.connect(DB_URL) as conn:
            # 1) Read XML + default language from the assets row
            xml_bytes, asset_lang = fetch_asset_xml_and_lang(conn, args.asset_id)

            # 2) Parse parts from XML
            rows = parse_xml_rows(xml_bytes)
            if not rows:
                print("No rows found.", file=sys.stderr)
                sys.exit(1)

            # 4) Upsert parts
            written = write_rows(
                conn=conn,
                asset_id=args.asset_id,
                rows=rows,
                language_code=asset_lang,
            )
            print(f"Inserted/updated {written} parts for asset_id={args.asset_id}.")

    except Exception as e:
        print(f"Failure: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
