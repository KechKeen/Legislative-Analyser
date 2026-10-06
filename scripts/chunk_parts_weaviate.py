#!/usr/bin/env python3

import os
import re
import psycopg
import sys
import tiktoken
import weaviate

OPENAI_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
CHUNK_SIZE_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50

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

weaviate_client = weaviate.connect_to_local(port=int(WEAVIATE_PORT))

encoding = tiktoken.encoding_for_model(OPENAI_MODEL)


def normalize_text(s: str) -> str:
    s = s.replace("\xa0", " ")  # non-breaking space -> normal space
    s = re.sub(r"\s+", " ", s)  # collapse whitespace
    s = re.sub(r"[^\w\s]", "", s)  # remove punctuation
    return s.lower().strip()


def chunk_text(text: str, chunk_size=CHUNK_SIZE_TOKENS, overlap=CHUNK_OVERLAP_TOKENS):
    tokens = encoding.encode(text)
    start = 0
    while start < len(tokens):
        end = start + chunk_size
        chunk_tokens = tokens[start:end]
        yield encoding.decode(chunk_tokens)
        start += chunk_size - overlap


def main():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        with conn.cursor() as read_cur:
            read_cur.execute("SELECT id, part_text FROM asset_parts ORDER BY id")

            collection = weaviate_client.collections.use("AssetPartChunk")

            with collection.batch.fixed_size(batch_size=200) as batch:
                for part_id, part_text in read_cur:
                    norm_text = normalize_text(part_text)

                    for idx, chunk in enumerate(chunk_text(norm_text)):
                        batch.add_object(
                            {
                                "asset_part_id": str(part_id),
                                "chunk_index": idx,
                                "chunk_text_normalized": chunk,
                            }
                        )

                    print(f"Queued part_id={part_id}")

                    if batch.number_errors > 10:
                        print("Batch import stopped due to excessive errors.")
                        break

            failed_objects = collection.batch.failed_objects
            if failed_objects:
                print(f"Number of failed imports: {len(failed_objects)}")
                print(f"First failed object: {failed_objects[0]}")

            weaviate_client.close()

    print("Import complete.")


if __name__ == "__main__":
    main()
