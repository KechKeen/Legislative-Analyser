#!/usr/bin/env python3
import os
import psycopg
import sys

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


def main() -> None:
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        # list user tables
        cur.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema NOT IN ('pg_catalog','information_schema')
            ORDER BY table_schema, table_name;
        """
        )
        for schema, table in cur.fetchall():
            print(f"\n=== {schema}.{table} ===")
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position;
            """,
                (schema, table),
            )
            for col, dtype in cur.fetchall():
                print(f" - {col}: {dtype}")


if __name__ == "__main__":
    main()
