#!/usr/bin/env python3

import os
import re
import psycopg
import sys
import tiktoken
from openai import OpenAI

OPENAI_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
CHUNK_SIZE_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50

DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("ADMIN_USER")
DB_PASSWORD = os.getenv("ADMIN_PASSWORD")
DB_HOST = os.getenv("POSTGRES_HOST")
DB_PORT = os.getenv("POSTGRES_PORT")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not all([DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, OPENAI_API_KEY]):
    print(
        "ERROR: Set POSTGRES_DB, ADMIN_USER, ADMIN_PASSWORD, POSTGRES_HOST, POSTGRES_PORT, and OPENAI_API_KEY env vars.",
        file=sys.stderr,
    )
    sys.exit(1)
DB_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

client = OpenAI(api_key=OPENAI_API_KEY)
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


def embed_text(text: str):
    resp = client.embeddings.create(model=OPENAI_MODEL, input=text)
    return resp.data[0].embedding


def main():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        with conn.cursor() as read_cur, conn.cursor() as write_cur:
            read_cur.execute("SELECT id, part_text FROM asset_parts ORDER BY id")
            for part_id, part_text in read_cur:
                norm_text = normalize_text(part_text)
                for idx, chunk in enumerate(chunk_text(norm_text)):
                    emb = embed_text(chunk)
                    write_cur.execute(
                        """
                        INSERT INTO asset_part_chunks
                        (asset_part_id, chunk_index, chunk_text_normalized, embedding)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (part_id, idx, chunk, emb),
                    )

                print(f"Processed part_id={part_id}")


if __name__ == "__main__":
    main()
