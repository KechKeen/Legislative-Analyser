#!/usr/bin/env python3

import argparse
import os
import sys
import psycopg
import json
import weaviate

DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("ADMIN_USER")
DB_PASSWORD = os.getenv("ADMIN_PASSWORD")
DB_HOST = os.getenv("POSTGRES_HOST")
DB_PORT = os.getenv("POSTGRES_PORT")
WEAVIATE_PORT = os.getenv("WEAVIATE_PORT")

if not all([DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, WEAVIATE_PORT]):
    print(
        "ERROR: Set POSTGRES_DB, ADMIN_USER, ADMIN_PASSWORD, POSTGRES_HOST, POSTGRES_PORT, and WEAVIATE_PORT env vars.",
        file=sys.stderr,
    )
    sys.exit(1)
DB_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

GET_OBLIGATIONS_SQL = """
SELECT
    o.id,
    o.run_id,
    o.source,
    o.article_id,
    o.polarity,
    o.obligation,
    o.obligation_references,
    o.created_at,
    a.asset_id,
    ast.asset_type,
    ast.title
FROM obligations o
JOIN articles a ON a.id = o.article_id
JOIN assets ast ON ast.id = a.asset_id
WHERE a.asset_id = %s
ORDER BY o.id
"""


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Sync obligations from PostgreSQL to Weaviate for a given asset."
    )
    ap.add_argument("--asset-id", type=int, required=True, help="Asset ID to sync obligations for")
    return ap.parse_args()


def main():
    args = parse_args()
    asset_id = args.asset_id

    weaviate_client = weaviate.connect_to_local(port=int(WEAVIATE_PORT))  # type: ignore[arg-type]

    try:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(GET_OBLIGATIONS_SQL, (asset_id,))
                rows = cur.fetchall()

                if not rows:
                    print(f"No obligations found for asset_id={asset_id}")
                    return

                collection = weaviate_client.collections.use("Obligation")

                with collection.batch.fixed_size(batch_size=100) as batch:
                    for row in rows:
                        (
                            ob_id,
                            run_id,
                            source,
                            article_id,
                            polarity,
                            obligation,
                            obligation_refs,
                            created_at,
                            asset_id_val,
                            asset_type,
                            asset_title,
                        ) = row

                        refs_str = None if obligation_refs is None else json.dumps(obligation_refs)

                        batch.add_object(
                            {
                                "obligation_id": ob_id,
                                "run_id": run_id,
                                "source": source,
                                "article_id": article_id,
                                "polarity": polarity,
                                "obligation": obligation,
                                "obligation_references": refs_str,
                                "created_at": created_at.isoformat(),
                                "asset_id": asset_id_val,
                                "asset_type": asset_type,
                                "asset_title": asset_title,
                            }
                        )

                        print(f"Queued obligation_id={ob_id}")

                        if batch.number_errors > 10:
                            print("Batch import stopped due to excessive errors.")
                            break

                failed_objects = collection.batch.failed_objects
                if failed_objects:
                    print(f"Number of failed imports: {len(failed_objects)}")
                    print(f"First failed object: {failed_objects[0]}")
                else:
                    print(f"Successfully imported {len(rows)} obligations for asset_id={asset_id}")

    finally:
        weaviate_client.close()


if __name__ == "__main__":
    main()
