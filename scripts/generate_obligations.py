#!/usr/bin/env python3
import argparse
import uuid
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple, cast

import psycopg
from openai import OpenAI

# --- DB config ---
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
MODEL = "gpt-5-mini"
REFERENCED_ARTICLES_MAX_DEPTH = 3

# --- SQL ---
GET_ARTICLE_SQL = """
SELECT id, asset_id, article_number, article_text
FROM articles
WHERE id = %s
"""

GET_DEFINED_TERMS_SQL = """
SELECT term, definition
FROM defined_terms
WHERE asset_id = %s
ORDER BY id
"""

GET_DIRECT_REFS_SQL = """
SELECT ar.to_article_id
FROM article_relations ar
JOIN articles a_from ON a_from.id = ar.from_article_id
JOIN articles a_to ON a_to.id = ar.to_article_id
WHERE a_from.asset_id = %s
  AND ar.from_article_id = %s
  AND a_to.asset_id = %s
  AND ar.relation_type = 'references'
ORDER BY ar.id
"""

GET_REFERENCED_ARTICLES_SQL = """
SELECT id, asset_id, article_number, article_text
FROM articles
WHERE id = ANY(%s)
ORDER BY article_number
"""

INSERT_OBLIGATION_SQL = """
INSERT INTO obligations (run_id, source, article_id, polarity, obligation, obligation_references)
VALUES (%s, %s, %s, %s, %s, %s)
"""

# Fetch all article ids for a given asset
GET_ARTICLE_IDS_BY_ASSET_SQL = """
SELECT id
FROM articles
WHERE asset_id = %s
ORDER BY article_number
"""


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Extract obligations for all articles within a single asset and save to DB."
    )
    ap.add_argument(
        "--asset-id", type=int, required=True, help="assets.id whose articles to process"
    )
    return ap.parse_args()


def fetch_main_article(conn: psycopg.Connection, article_id: int) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(GET_ARTICLE_SQL, (article_id,))
        row = cur.fetchone()
        if not row or not row[3]:
            return None
        return {
            "id": row[0],
            "asset_id": row[1],
            "article_number": row[2],
            "article_text": row[3],
        }


def fetch_defined_terms(conn: psycopg.Connection, asset_id: int) -> List[Tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(GET_DEFINED_TERMS_SQL, (asset_id,))
        return [(r[0], r[1]) for r in cur.fetchall()]


# TODO: Optimize with recursive CTE in SQL
def fetch_reference_ids_same_asset(
    conn: psycopg.Connection, asset_id: int, article_id: int, max_depth: int
) -> List[int]:
    result: List[int] = []
    visited: set[int] = {article_id}
    queue: List[Tuple[int, int]] = [(article_id, 0)]

    with conn.cursor() as cur:
        while queue:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue

            cur.execute(GET_DIRECT_REFS_SQL, (asset_id, current, asset_id))
            rows = cur.fetchall()
            next_depth = depth + 1
            for (to_id,) in rows:
                if to_id in visited:
                    continue

                visited.add(to_id)
                result.append(to_id)

                if next_depth < max_depth:
                    queue.append((to_id, next_depth))

    return result


def fetch_articles_by_ids(conn: psycopg.Connection, article_ids: List[int]) -> List[Dict[str, Any]]:
    if not article_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(GET_REFERENCED_ARTICLES_SQL, (article_ids,))
        rows = cur.fetchall()
    return [
        {"id": r[0], "asset_id": r[1], "article_number": r[2], "article_text": r[3]}
        for r in rows
        if r[3]
    ]


def build_tagged_payload(
    main_article: Dict[str, Any],
    terms: List[Tuple[str, str]],
    referenced_articles: List[Dict[str, Any]],
) -> str:
    # --- <main article> ---
    main_block = [
        f"Article {main_article.get('article_number')}",
        "",
        main_article.get("article_text", "").strip(),
    ]
    main_str = "\n".join(main_block).strip()

    # --- <defined terms> ---
    if terms:
        dt_lines = []
        for t, d in terms:
            dt_lines.append(f"- {t}: {d}")
        terms_str = "\n".join(dt_lines)
    else:
        terms_str = "(none)"

    # --- <referenced articles> ---
    if referenced_articles:
        ref_chunks = []
        for ra in referenced_articles:
            ref_chunks.append(
                f"[Article {ra.get('article_number')}] { (ra.get('article_text') or '').strip() }"
            )
        refs_str = "\n\n---\n\n".join(ref_chunks)
    else:
        refs_str = "(none)"

    # Final tagged text
    payload = (
        "<main article>\n"
        f"{main_str}\n"
        "</main article>\n\n"
        "<defined terms>\n"
        f"{terms_str}\n"
        "</defined terms>\n\n"
        "<referenced articles>\n"
        f"{refs_str}\n"
        "</referenced articles>\n"
        "Now extract all binding legal obligations from the main article."
    )
    return payload


def call_openai_for_obligations(tagged_payload: str) -> List[Dict[str, Any]]:
    """Call OpenAI with structured outputs to extract obligations.

    Returns a list of dicts with keys: polarity, obligation, obligation_references.
    """
    client = OpenAI(api_key=OPENAI_API_KEY)

    system_text = (
        "You are a legal analysis model. "
        "Your task is to extract all binding legal obligations from a legislative text and return them as a json object.\n"
        "\n"
        "Your goal is to identify every clause that creates a binding legal obligation — either positive or negative.\n"
        "Categories of binding obligations:\n"
        "1. positive - mandatory requirements to act (e.g., 'shall', 'must', 'is required to', 'ensure').\n"
        "2. negative - mandatory requirements not to act (e.g., 'shall not', 'is prohibited', 'must not').\n"
        "\n"
        "\n"
        "You will receive three tagged sections: <main article>, <defined terms>, and <referenced articles>.\n"
        "\n"
        "- <main article>: The primary article from which you must extract obligations.\n"
        "- <defined terms>: Any definitions that may or may not be referenced in the main article.\n"
        "- <referenced articles>: Any other articles that may be referenced in the main article.\n"
        "\n"
        "\n"
        "Follow this extraction procedure:\n"
        "1. Read the main article text, as well as any referenced definitions or articles.\n"
        "2. Identify each clause in <main article> that contains a deontic verb or phrase indicating a binding obligation"
        " (e.g., 'shall', 'must', 'shall not').\n"
        "3. Determine whether the obligation is positive (to act) or negative (not to act) and set 'polarity' accordingly.\n"
        "4. Write a short plain-language summary of the obligation in the 'obligation' field that fully captures the meaning of the legal rule."
        " Ensure the summary includes who the obligation applies to, what must or must not be done, under what conditions or triggers it applies,"
        " and any deadlines, exceptions, or qualifiers necessary to understand the rule completely.\n"
        "5. For each obligation, record every relevant paragraph, point, or subpoint of <main article>, <defined terms>, and <referenced articles>"
        " that establishes or clarifies the obligation in 'obligation_references'."
        " Also include any relevant sections referred to in the obligation summary."
        " Each reference must include both the specific article identifier and the exact text of the legal clause."
        " Include all and only the text you relied on to construct the obligation and avoid copying unrelated sections."
        " Keep each reference as short and focused as possible.\n"
        "6. Return a valid JSON object conforming exactly to the schema. The object must contain a single key 'obligations'"
        " whose value is an array of obligation objects.\n"
        "\n"
        "\n"
        "Each clause must be stated or implied by the <main article>."
        " Use <defined terms> and <referenced articles> only as clarifying context."
        " Do NOT include obligations that rely solely on <referenced articles> if they are not required by the main article.\n"
        "\n"
        "Ensure all obligations are included. Do not omit any binding obligations.\n"
    )

    schema = {
        "name": "obligations_list",
        "type": "json_schema",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "obligations": {
                    "type": "array",
                    "description": "A list of legal obligations extracted from the main article.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "description": "A legal obligation extracted from the main article.",
                        "properties": {
                            "polarity": {
                                "type": "string",
                                "enum": ["positive", "negative"],
                                "description": (
                                    "Indicates the nature of the legal obligation. "
                                    "'positive' = mandatory requirement to act; "
                                    "'negative' = mandatory requirement not to act."
                                ),
                            },
                            "obligation": {
                                "type": "string",
                                "description": (
                                    "A short plain-language summary of the obligation. "
                                    "Include who the obligation applies to, what must or must not be done, "
                                    "under what conditions or triggers it applies, and any deadlines, "
                                    "exceptions, or qualifiers necessary to understand the rule."
                                ),
                            },
                            "obligation_references": {
                                "type": "array",
                                "description": (
                                    "All legal text used to derive and clarify this obligation, "
                                    "as well as any relevant sections referred to in the obligation summary. "
                                    "Include only the relevant sentences, paragraphs, points, or definitions "
                                    "from <main article>, <defined terms>, or <referenced articles> that "
                                    "establish or qualify the obligation. Keep each reference as short and "
                                    "focused as possible."
                                ),
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "article_id": {
                                            "type": "string",
                                            "description": (
                                                "The identifier of the referenced passage, such as "
                                                "'Article 10(2)', 'Article 33(1)(a)', or "
                                                "'Annex I, paragraph 2'."
                                            ),
                                        },
                                        "text": {
                                            "type": "string",
                                            "description": (
                                                "The exact text of the legal clause used to generate "
                                                "the obligation. Do not paraphrase, summarise, or alter "
                                                "the wording."
                                            ),
                                        },
                                    },
                                    "required": ["article_id", "text"],
                                },
                            },
                        },
                        "required": ["polarity", "obligation", "obligation_references"],
                    },
                }
            },
            "required": ["obligations"],
        },
    }

    inputs = [
        {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
        {"role": "user", "content": [{"type": "input_text", "text": tagged_payload}]},
    ]

    resp = client.responses.create(
        model=MODEL,
        input=cast(Any, inputs),
        text=cast(Any, {"format": schema}),
    )

    raw = (resp.output_text or "").strip()
    if not raw:
        return []

    try:
        # Structured outputs should be valid JSON matching the schema
        data = json.loads(raw)
        # Expect an object with key 'obligations' containing the list
        if isinstance(data, dict):
            items = data.get("obligations", [])
            if isinstance(items, list):
                return items
        return []
    except Exception as e:
        print(f"Warning: Failed to parse structured output: {e}", file=sys.stderr)
        return []


def insert_obligations(
    conn: psycopg.Connection,
    article_id: int,
    obligations: List[Dict[str, Any]],
    run_id: uuid.UUID,
) -> int:
    """Insert structured obligations into the database.

    Maps structured obligation data to the obligations table:
    - polarity: from obligation['polarity'] ('positive' or 'negative')
    - obligation: from obligation['obligation']
    - obligation_references: from obligation['obligation_references'] as JSONB
    """
    if not obligations:
        return 0
    with conn.cursor() as cur:
        for ob in obligations:
            polarity = ob.get("polarity", "").strip()
            obligation_text = ob.get("obligation", "").strip()
            if not polarity or not obligation_text:
                continue
            # Store references as JSONB (psycopg handles the conversion)
            refs = ob.get("obligation_references", [])
            cur.execute(
                INSERT_OBLIGATION_SQL,
                (run_id, "asset", article_id, polarity, obligation_text, json.dumps(refs or [])),
            )
    conn.commit()
    return len(obligations)


def process_single_article(
    asset_id: int, article_id: int, run_id: uuid.UUID
) -> Tuple[int, int, int]:
    """Process a single article and return (article_id, num_obligations, num_refs) for tracking."""
    # Each thread gets its own connection
    with psycopg.connect(DB_URL) as conn:
        main_article = fetch_main_article(conn, article_id)
        if not main_article:
            raise RuntimeError(f"Article id={article_id} not found or empty.")

        terms = fetch_defined_terms(conn, asset_id)
        ref_ids = fetch_reference_ids_same_asset(
            conn, asset_id, article_id, max_depth=REFERENCED_ARTICLES_MAX_DEPTH
        )
        ref_articles = fetch_articles_by_ids(conn, ref_ids)

        tagged_payload = build_tagged_payload(main_article, terms, ref_articles)
        obligations = call_openai_for_obligations(tagged_payload)

        n = insert_obligations(conn, article_id, obligations, run_id)

        return (article_id, n, len(ref_ids))


def main():
    args = parse_args()
    asset_id: int = args.asset_id
    run_id = uuid.uuid4()

    try:
        # Fetch all article ids for this asset
        with psycopg.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(GET_ARTICLE_IDS_BY_ASSET_SQL, (asset_id,))
                article_rows = cur.fetchall()

        if not article_rows:
            print(f"ERROR: No articles found for asset_id={asset_id}.", file=sys.stderr)
            sys.exit(2)

        article_ids = [row[0] for row in article_rows]
        print(f"Processing {len(article_ids)} articles")

        # Process articles in parallel with 10 workers
        total_obligations = 0
        success_count = 0
        error_count = 0

        with ThreadPoolExecutor(max_workers=10) as executor:
            # Submit all tasks
            future_to_article = {
                executor.submit(process_single_article, asset_id, article_id, run_id): article_id
                for article_id in article_ids
            }

            # Process completed tasks as they finish
            for future in as_completed(future_to_article):
                article_id = future_to_article[future]
                try:
                    article_id_result, n_obligations, n_refs = future.result()
                    success_count += 1
                    total_obligations += n_obligations
                    print(
                        f"[{success_count}/{len(article_ids)}] Article {article_id_result}: "
                        f"{n_obligations} obligation(s), {n_refs} refs"
                    )
                except Exception as e:
                    error_count += 1
                    print(
                        f"[{success_count + error_count}/{len(article_ids)}] "
                        f"Failure processing article_id={article_id}: {e}",
                        file=sys.stderr,
                    )

        print(
            f"\nCompleted: {success_count} articles processed, {error_count} errors, "
            f"{total_obligations} total obligations for asset_id={asset_id}, run_id={run_id}."
        )

    except Exception as e:
        print(f"Failure: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
