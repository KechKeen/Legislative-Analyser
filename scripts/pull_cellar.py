import os
import sys
import hashlib
from pathlib import Path
import requests
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

CELEX_RESOURCES = [
    {
        "title": "Consolidated text: Regulation (EU) 2016/679 of the European Parliament and of the Council of 27 April 2016 on the protection of natural persons with regard to the processing of personal data and on the free movement of such data, and repealing Directive 95/46/EC (General Data Protection Regulation) (Text with EEA relevance)",
        "celex": "02016R0679-20160504",
        "source_uri": "https://publications.europa.eu/resource/cellar/5f2552c2-cc45-11e6-ad7c-01aa75ed71a1.0022.01/DOC_1",
        "variant": "5f2552c2-cc45-11e6-ad7c-01aa75ed71a1.0022.01/DOC_1",
    },
    {
        "title": "[AI Act] REGULATION (EU) 2024/1689 OF THE EUROPEAN PARLIAMENT AND OF THE COUNCIL ",
        "celex": "32024R1689",
        "source_uri": None,
        "variant": None,
        "local_file": "32024R1689.xml",
    },
]


def read_local_xml(filename: str) -> bytes:
    """Read XML file from ../dev/documents/"""
    script_dir = Path(__file__).resolve().parent
    file_path = script_dir.parent / "dev" / "documents" / filename

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "rb") as f:
        return f.read()


def download_xml(source_uri: str) -> requests.Response:
    headers = {"Accept": "application/xml;type=fmx4", "Accept-Language": "en"}
    return requests.get(source_uri, headers=headers, timeout=60)


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


UPSERT_ASSET_SQL = """
INSERT INTO assets (
  source, source_id, variant, language_code,
  source_uri, source_published_at,
  asset_type, title, asset_text, storage_uri, content_hash
) VALUES (
  %(source)s, %(source_id)s, %(variant)s, %(language_code)s,
  %(source_uri)s, %(source_published_at)s,
  %(asset_type)s, %(title)s, %(asset_text)s, %(storage_uri)s, %(content_hash)s
)
ON CONFLICT ON CONSTRAINT uq_asset
DO UPDATE SET
  updated_at = now(),
  source_uri = EXCLUDED.source_uri,
  source_published_at = COALESCE(EXCLUDED.source_published_at, assets.source_published_at),
  asset_type = EXCLUDED.asset_type,
  title = COALESCE(EXCLUDED.title, assets.title),
  asset_text = EXCLUDED.asset_text,
  storage_uri = EXCLUDED.storage_uri,
  content_hash = EXCLUDED.content_hash
RETURNING id;
"""


def main():
    with psycopg.connect(DB_URL, autocommit=False) as conn:
        with conn.cursor() as cur:
            for rec in CELEX_RESOURCES:
                title = rec["title"]
                celex = rec["celex"]
                source_uri = rec["source_uri"]
                variant = rec["variant"]

                print(f"\n=== {celex} ===")

                if "local_file" in rec:
                    try:
                        xml_bytes = read_local_xml(rec["local_file"])
                    except FileNotFoundError as e:
                        print(f"Error: {e} — skipping")
                        continue
                else:
                    r = download_xml(source_uri)
                    if r.status_code != 200:
                        print(f"FMX4 HTTP {r.status_code} — skipping")
                        conn.rollback()
                        continue
                    xml_bytes = r.content
                content_hash = sha256_hex(xml_bytes)

                params = {
                    "source": "eur-lex",
                    "source_id": celex,
                    "variant": variant,
                    "language_code": "en",
                    "source_uri": source_uri,
                    "source_published_at": None,  # todo
                    "asset_type": "legislation",
                    "title": title,
                    "asset_text": xml_bytes.decode("utf-8", errors="replace"),
                    "storage_uri": None,  # not storing externally for now
                    "content_hash": content_hash,
                }

                cur.execute(UPSERT_ASSET_SQL, params)
                asset_id = cur.fetchone()[0]
                conn.commit()
                print(f"Upserted assets.id = {asset_id}")


if __name__ == "__main__":
    main()
