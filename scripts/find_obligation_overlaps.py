#!/usr/bin/env python3

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple, cast
import threading

import psycopg
import weaviate
from openai import OpenAI
from FlagEmbedding import FlagReranker

# --- DB config ---
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("ADMIN_USER")
DB_PASSWORD = os.getenv("ADMIN_PASSWORD")
DB_HOST = os.getenv("POSTGRES_HOST")
DB_PORT = os.getenv("POSTGRES_PORT")
WEAVIATE_PORT = os.getenv("WEAVIATE_PORT")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not all([DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, WEAVIATE_PORT, OPENAI_API_KEY]):
    print(
        "ERROR: Set POSTGRES_DB, ADMIN_USER, ADMIN_PASSWORD, POSTGRES_HOST, POSTGRES_PORT, WEAVIATE_PORT, and OPENAI_API_KEY env vars.",
        file=sys.stderr,
    )
    sys.exit(1)
DB_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MODEL = "gpt-5-mini"

# --- Search parameters ---
HYBRID_LIMIT = 50
RERANK_TOP_K = 10

# --- SQL queries ---
GET_SOURCE_OBLIGATION_SQL = """
SELECT
    o.id,
    o.obligation,
    o.polarity,
    o.article_id,
    a.asset_id,
    ast.title as asset_title
FROM obligations o
JOIN articles a ON a.id = o.article_id
JOIN assets ast ON ast.id = a.asset_id
WHERE o.id = %s
"""

INSERT_OBLIGATION_RELATION_SQL = """
INSERT INTO obligation_relations (from_obligation_id, to_obligation_id, relation_type, reasoning, scores)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""


GET_SOURCE_ASSET_OBLIGATION_IDS_SQL = """
SELECT o.id
FROM obligations o
JOIN articles a ON a.id = o.article_id
WHERE a.asset_id = %s
ORDER BY o.id
"""


RERANKER = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True)
RERANKER_LOCK = threading.Lock()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Find overlapping obligations by comparing each obligation from a source asset's articles "
            "against obligations from a destination asset."
        )
    )
    ap.add_argument(
        "--source-asset-id",
        type=int,
        required=True,
        help="assets.id whose article obligations will be used as sources",
    )
    ap.add_argument(
        "--dest-asset-id",
        type=int,
        required=True,
        help="assets.id to search for overlapping obligations (destination)",
    )
    return ap.parse_args()


def fetch_source_obligation(conn: psycopg.Connection, obligation_id: int) -> Dict[str, Any]:
    """Fetch the source obligation from the database."""
    with conn.cursor() as cur:
        cur.execute(GET_SOURCE_OBLIGATION_SQL, (obligation_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Obligation with id={obligation_id} not found")

        return {
            "id": row[0],
            "obligation": row[1],
            "polarity": row[2],
            "article_id": row[3],
            "asset_id": row[4],
            "asset_title": row[5],
        }


def hybrid_search_weaviate(
    weaviate_client: weaviate.WeaviateClient, query_text: str, asset_id: int, limit: int
) -> List[Dict[str, Any]]:
    """Perform hybrid search on Weaviate obligations collection for a specific asset."""
    collection = weaviate_client.collections.get("Obligation")

    response = collection.query.hybrid(
        query=query_text,
        limit=limit,
        alpha=0.6,
        filters=weaviate.classes.query.Filter.by_property("asset_id").equal(asset_id),
        return_metadata=weaviate.classes.query.MetadataQuery(score=True, explain_score=True),
    )

    results = []
    for item in response.objects:
        results.append(
            {
                "obligation_id": item.properties.get("obligation_id"),
                "obligation": item.properties.get("obligation"),
                "polarity": item.properties.get("polarity"),
                "article_id": item.properties.get("article_id"),
                "asset_id": item.properties.get("asset_id"),
                "asset_title": item.properties.get("asset_title"),
                "hybrid_score": item.metadata.score if item.metadata else None,
                "hybrid_explain_score": item.metadata.explain_score if item.metadata else None,
            }
        )

    return results


# TODO: consider including references in the rerank step and LLM step (hybrid search step would be too noisy?)
def rerank_results(
    source_obligation: str, candidate_obligations: List[Dict[str, Any]], top_k: int
) -> List[Dict[str, Any]]:
    if not candidate_obligations:
        return []

    pairs = [[source_obligation, cand["obligation"]] for cand in candidate_obligations]

    with RERANKER_LOCK:
        scores = RERANKER.compute_score(pairs)

    scored_candidates = [
        {**cand, "rerank_score": float(score)} for cand, score in zip(candidate_obligations, scores)
    ]
    scored_candidates.sort(key=lambda x: x["rerank_score"], reverse=True)

    return scored_candidates[:top_k]


def compare_obligations_with_llm(
    source_obligation: Dict[str, Any], candidate_obligations: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not candidate_obligations:
        return []

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Build the prompt
    system_text = (
        "You are a legal analysis model specialized in comparing legal obligations.\n"
        "\n"
        "Your task is to identify SIGNIFICANT overlaps between a source obligation and a list of candidate obligations. "
        "An overlap is considered SIGNIFICANT when the two obligations could be efficiently merged into a single obligation "
        "such that the final set of obligations would be more concise without loss of legal meaning or precision.\n"
        "\n"
        "Consider an overlap significant if:\n"
        "- The obligations impose substantially the same requirement on the same or similar entities\n"
        "- The obligations address the same subject matter with similar conditions and triggers\n"
        "- Merging them would reduce redundancy while preserving all essential legal requirements\n"
        "- The obligations are complementary and could be combined into a more comprehensive single statement\n"
        "\n"
        "Do NOT consider minor similarities or tangential relationships as significant overlaps.\n"
        "\n"
        "For each candidate obligation, return:\n"
        "- has_significant_overlap: boolean (true if there is a significant overlap)\n"
        "- reasoning: detailed explanation of why there is or isn't a significant overlap\n"
        "\n"
        "You MUST return exactly one entry for each candidate obligation, even if there is no overlap.\n"
    )

    # Format candidate obligations for the prompt
    candidates_text = ""
    for i, cand in enumerate(candidate_obligations, 1):
        candidates_text += f"\n[Candidate {i}]\n"
        candidates_text += f"Text: {cand['obligation']}\n"

    user_text = (
        f"SOURCE OBLIGATION:\n"
        f"Text: {source_obligation['obligation']}\n"
        f"\n"
        f"CANDIDATE OBLIGATIONS:\n"
        f"{candidates_text}\n"
        f"\n"
        f"Analyze each candidate obligation and determine if there is a significant overlap with the source obligation."
    )

    # Define the JSON schema for structured output
    schema = {
        "name": "overlap_analysis",
        "type": "json_schema",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "comparisons": {
                    "type": "array",
                    "description": "Analysis results for each candidate obligation",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "candidate_number": {
                                "type": "integer",
                                "description": "The candidate number being analyzed",
                            },
                            "has_significant_overlap": {
                                "type": "boolean",
                                "description": "Whether there is a significant overlap between the source and this candidate",
                            },
                            "reasoning": {
                                "type": "string",
                                "description": "Detailed explanation of why there is or isn't a significant overlap",
                            },
                        },
                        "required": ["candidate_number", "has_significant_overlap", "reasoning"],
                    },
                }
            },
            "required": ["comparisons"],
        },
    }

    inputs = [
        {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
        {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
    ]

    resp = client.responses.create(
        model=MODEL,
        input=cast(Any, inputs),
        text=cast(Any, {"format": schema}),
    )

    raw = (resp.output_text or "").strip()
    if not raw:
        print("Warning: Empty response from LLM", file=sys.stderr)
        return []

    try:
        data = json.loads(raw)
        comparisons = data.get("comparisons", [])

        # Map the results back to the candidate obligations
        results = []
        for comp in comparisons:
            candidate_idx = comp.get("candidate_number", 0) - 1  # Convert to 0-indexed
            if 0 <= candidate_idx < len(candidate_obligations):
                cand = candidate_obligations[candidate_idx]
                results.append(
                    {
                        "obligation_id": cand["obligation_id"],
                        "has_significant_overlap": comp.get("has_significant_overlap", False),
                        "reasoning": comp.get("reasoning", ""),
                        "hybrid_score": cand.get("hybrid_score"),
                        "rerank_score": cand.get("rerank_score"),
                        "hybrid_explain_score": cand.get("hybrid_explain_score"),
                    }
                )

        return results

    except Exception as e:
        print(f"Warning: Failed to parse LLM output: {e}", file=sys.stderr)
        return []


def save_obligation_relations(
    conn: psycopg.Connection, source_obligation_id: int, overlap_results: List[Dict[str, Any]]
) -> int:
    count = 0
    with conn.cursor() as cur:
        for result in overlap_results:
            if result.get("has_significant_overlap"):
                # Prepare scores JSONB
                scores = {
                    "hybrid_score": result.get("hybrid_score"),
                    "rerank_score": result.get("rerank_score"),
                    "hybrid_explain_score": result.get("hybrid_explain_score"),
                }

                cur.execute(
                    INSERT_OBLIGATION_RELATION_SQL,
                    (
                        source_obligation_id,
                        result["obligation_id"],
                        "overlaps",
                        result.get("reasoning", ""),
                        json.dumps(scores),
                    ),
                )
                count += 1
    conn.commit()
    return count


def process_single_obligation(
    source_obligation_id: int, dest_asset_id: int
) -> Tuple[int, int, int]:
    # Each thread creates its own DB and Weaviate clients
    weaviate_client = weaviate.connect_to_local(port=int(WEAVIATE_PORT))
    try:
        with psycopg.connect(DB_URL, autocommit=False) as conn:
            source_obligation = fetch_source_obligation(conn, source_obligation_id)

            search_results = hybrid_search_weaviate(
                weaviate_client,
                source_obligation["obligation"],
                dest_asset_id,
                limit=HYBRID_LIMIT,
            )
            if not search_results:
                return (source_obligation_id, 0, 0)

            reranked_results = rerank_results(
                source_obligation["obligation"], search_results, top_k=RERANK_TOP_K
            )

            overlap_results = compare_obligations_with_llm(source_obligation, reranked_results)

            saved_count = save_obligation_relations(conn, source_obligation_id, overlap_results)

            return (source_obligation_id, saved_count, len(reranked_results))
    finally:
        weaviate_client.close()


def main():
    args = parse_args()
    source_asset_id: int = args.source_asset_id
    dest_asset_id: int = args.dest_asset_id

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(GET_SOURCE_ASSET_OBLIGATION_IDS_SQL, (source_asset_id,))
            rows = cur.fetchall()

    if not rows:
        print(f"No obligations found for source asset_id={source_asset_id}.")
        return

    obligation_ids = [r[0] for r in rows]
    print(f"Processing {len(obligation_ids)} obligations from source asset {source_asset_id}...")

    total_saved = 0
    processed = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ob = {
            executor.submit(process_single_obligation, ob_id, dest_asset_id): ob_id
            for ob_id in obligation_ids
        }

        for future in as_completed(future_to_ob):
            ob_id = future_to_ob[future]
            try:
                src_ob_id, saved_count, candidates = future.result()
                processed += 1
                total_saved += saved_count
                print(
                    f"[{processed}/{len(obligation_ids)}] Obligation {src_ob_id}: "
                    f"saved {saved_count} overlap(s) from {candidates} candidates"
                )
            except Exception as e:
                errors += 1
                print(
                    f"[{processed + errors}/{len(obligation_ids)}] Failure processing obligation_id={ob_id}: {e}",
                    file=sys.stderr,
                )

    print(
        f"\nCompleted: {processed} obligations processed, {errors} errors, "
        f"{total_saved} overlap relations saved (dest_asset_id={dest_asset_id})."
    )


if __name__ == "__main__":
    main()
