import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any, Dict, List, TypedDict, cast

from pydantic import BaseModel
from fastapi import FastAPI, Query
import weaviate
from weaviate.classes.query import MetadataQuery
import psycopg
from openai import AsyncOpenAI
from fastapi.responses import HTMLResponse


class ChunkHit(TypedDict, total=False):
    uuid: str
    asset_part_id: str
    chunk_index: int
    chunk_text_normalized: str
    score: float


class RagRequest(BaseModel):
    query: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # --- Startup ---
    weaviate_client = weaviate.connect_to_local(
        host=os.getenv("WEAVIATE_HOST", "localhost"),
        port=int(os.getenv("WEAVIATE_PORT", "8080")),
        grpc_port=int(os.getenv("WEAVIATE_GRPC_PORT", "50051")),
    )
    pg_conn = psycopg.connect(
        f"postgresql://{os.getenv('ADMIN_USER')}:{os.getenv('ADMIN_PASSWORD')}@{os.getenv('DB_HOST')}/{os.getenv('POSTGRES_DB')}"
    )
    openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # Attach resources to app.state for reuse
    app.state.weaviate_client = weaviate_client
    app.state.chunks = weaviate_client.collections.get("AssetPartChunk")
    app.state.pg_conn = pg_conn
    app.state.openai_client = openai_client

    yield  # --- Application runs here ---

    # --- Shutdown ---
    weaviate_client.close()
    pg_conn.close()
    await openai_client.close()


app = FastAPI(lifespan=lifespan)


def _hybrid_chunks(q: str, limit: int = 3) -> List[ChunkHit]:
    chunks = app.state.chunks

    res = chunks.query.hybrid(
        query=q,
        query_properties=[
            "chunk_text_normalized"
        ],  # specify the fields to consider for keyword search (doesn't affect vector search)
        alpha=0.5,
        limit=limit,
        return_metadata=MetadataQuery(score=True),
    )

    out: List[ChunkHit] = []
    for obj in res.objects:
        out.append(
            {
                "uuid": str(obj.uuid),
                "asset_part_id": obj.properties.get("asset_part_id"),
                "chunk_index": obj.properties.get("chunk_index"),
                "chunk_text_normalized": obj.properties.get("chunk_text_normalized"),
                "score": float(obj.metadata.score or 0.0),
            }
        )
    return out


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/search/chunks")
async def search_chunks(q: str = Query(..., description="Search text")) -> List[ChunkHit]:
    return _hybrid_chunks(q, limit=3)


@app.get("/search")
async def search(q: str = Query(..., description="Search text")) -> List[Dict[str, Any]]:
    pg_conn = app.state.pg_conn

    chunks = _hybrid_chunks(q, limit=3)
    if not chunks:
        return []

    seen: set[str] = set()
    asset_part_ids: List[str] = []
    scores: Dict[str, float] = {}
    for ch in chunks:
        ap_id = ch.get("asset_part_id")
        if ap_id and ap_id not in seen:
            seen.add(ap_id)
            asset_part_ids.append(ap_id)
            scores[ap_id] = float(ch.get("score", 0.0))

    if not asset_part_ids:
        return []

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, asset_id, article_number, part_number, part_tag, part_text
            FROM asset_parts
            WHERE id = ANY(%s)
            """,
            (asset_part_ids,),
        )
        rows = cur.fetchall()

    parts: Dict[str, Any] = {str(r[0]): r for r in rows}

    results: List[Dict[str, Any]] = []
    for ap_id in asset_part_ids:
        row = parts.get(ap_id)
        if not row:
            continue
        _id, asset_id, article_number, part_number, part_tag, part_text = row
        results.append(
            {
                "asset_id": asset_id,
                "asset_part_id": str(_id),  # keep as string to match keys
                "score": scores[ap_id],
                "article_number": article_number,
                "part_number": part_number,
                "part_tag": part_tag,
                "part_text": part_text,
            }
        )

    return results


@app.post("/rag")
async def rag_search(request: RagRequest) -> Dict[str, Any]:
    """
    Simple RAG endpoint for GDPR legal assistant.
    Retrieves top relevant chunks from Weaviate, maps them to asset_parts,
    adds neighboring parts for context, builds a prompt, and queries GPT-5-mini.
    """
    q = request.query
    pg_conn = app.state.pg_conn
    openai_client = app.state.openai_client
    chunks = _hybrid_chunks(q, limit=5)
    if not chunks:
        return {"answer": "No relevant context found.", "context_parts": []}

    seen: set[str] = set()
    asset_part_ids: List[str] = []
    for ch in chunks:
        ap_id = ch.get("asset_part_id")
        if ap_id and ap_id not in seen:
            seen.add(ap_id)
            asset_part_ids.append(ap_id)

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, asset_id, part_index, article_number, part_number, part_tag, part_text
            FROM asset_parts
            WHERE id = ANY(%s)
            """,
            (asset_part_ids,),
        )
        rows = cur.fetchall()

        context_parts: List[Dict[str, Any]] = []
        parts_ids: set[int] = set()
        for _id, asset_id, part_index, _article_number, _part_number, _part_tag, _part_text in rows:
            # --- Fetch this part and its immediate neighbors separately ---
            cur.execute(
                """
                SELECT id, part_index, article_number, part_number, part_tag, part_text
                FROM asset_parts
                WHERE asset_id = %s AND part_index BETWEEN %s AND %s
                ORDER BY part_index
                """,
                (asset_id, part_index - 1, part_index + 1),
            )
            neighbor_rows = cur.fetchall()

            for p_id, _p_index, art_no, p_no, tag, text in neighbor_rows:
                if p_id and p_id not in parts_ids:
                    label_parts: list[str] = []
                    if art_no:
                        label_parts.append(f"Article {art_no}")
                    label_parts.append(tag.capitalize())
                    if p_no:
                        label_parts.append(str(p_no))
                    identifier = " ".join(label_parts)

                    context_parts.append(
                        {
                            "id": str(p_id),
                            "identifier": identifier,
                            "context_text": text,
                        }
                    )
                    parts_ids.add(p_id)

    context_str = "\n\n---\n\n".join(
        [f"[{p['identifier']}] {p['context_text']}" for p in context_parts]
    )

    inputs = [
        {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "You are a GDPR legal assistant. Use the provided context from the GDPR regulation "
                        "to answer the user's legal question precisely and concisely. "
                        "Cite article numbers when relevant.\n\n"
                        f"Context:\n{context_str}"
                    ),
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": q,
                }
            ],
        },
    ]

    response = await openai_client.responses.create(
        model="gpt-5",
        input=inputs,
    )

    return {
        "query": q,
        "answer": response.output_text or "",
        "context_parts": context_parts,
    }


@app.get("/ui", response_class=HTMLResponse)
def rag_ui() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>GDPR Assistant</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 900px; }
    form { display: flex; gap: .5rem; margin-bottom: 1rem; }
    input[type="text"] { flex: 1; padding: .6rem .8rem; }
    button { padding: .6rem .9rem; cursor: pointer; }
    .panel { border: 1px solid #8884; border-radius: 8px; padding: 1rem; margin-top: 1rem; }
    #answer { white-space: pre-wrap; }
    details { margin-top: .5rem; }
    .muted { opacity: .7; font-size: .9rem; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <h1>GDPR Assistant</h1>
  <form id="f">
    <input id="q" type="text" placeholder="Ask a GDPR question…" required />
    <button id="go" type="submit">Ask</button>
  </form>

  <div id="status" class="muted"></div>

  <div id="answerPanel" class="panel hidden">
    <h2>Answer</h2>
    <pre id="answer"></pre>
  </div>

  <details id="contextPanel" class="hidden">
    <summary><b>Context</b></summary>
    <div id="ctx"></div>
  </details>

  <script>
    const f = document.getElementById('f');
    const q = document.getElementById('q');
    const go = document.getElementById('go');
    const ans = document.getElementById('answer');
    const ctx = document.getElementById('ctx');
    const status = document.getElementById('status');
    const answerPanel = document.getElementById('answerPanel');
    const contextPanel = document.getElementById('contextPanel');

    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const query = q.value.trim();
      if (!query) return;

      go.disabled = true;
      status.textContent = 'Thinking…';
      ans.textContent = '';
      ctx.innerHTML = '';
      answerPanel.classList.add('hidden');
      contextPanel.classList.add('hidden');

      try {
        const res = await fetch('/rag', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: query })
        });
        if (!res.ok) throw new Error('Request failed: ' + res.status);
        const data = await res.json();

        ans.textContent = data.answer || '(no answer)';
        status.textContent = '';

        // Show panels only when data exists
        if (data.answer) answerPanel.classList.remove('hidden');
        if (data.context_parts && data.context_parts.length > 0) {
          const frag = document.createDocumentFragment();
          data.context_parts.forEach(p => {
            const div = document.createElement('div');
            div.style.marginBottom = '.75rem';
            const head = document.createElement('div');
            head.innerHTML = `<b>${p.identifier || '(unknown)'}</b> <span class="muted">#${p.id ?? ''}</span>`;
            const pre = document.createElement('pre');
            pre.style.whiteSpace = 'pre-wrap';
            pre.textContent = (p.context_text || '').slice(0, 1200);
            div.appendChild(head);
            div.appendChild(pre);
            frag.appendChild(div);
          });
          ctx.appendChild(frag);
          contextPanel.classList.remove('hidden');
        }
      } catch (err) {
        console.error(err);
        status.textContent = 'Error: ' + (err.message || err);
        ans.textContent = '';
      } finally {
        go.disabled = false;
      }
    });

    q.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) f.dispatchEvent(new Event('submit'));
    });
  </script>
</body>
</html>"""


@app.get("/obligations/ui", response_class=HTMLResponse)
async def obligations_ui() -> str:
    """Minimal UI to view all obligations grouped by article with collapsible sections."""
    pg_conn = app.state.pg_conn

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                o.id, o.article_id, o.polarity, o.obligation, o.obligation_references,
                a.article_number
            FROM obligations o
            JOIN articles a ON a.id = o.article_id
            ORDER BY a.article_number, o.id
        """
        )
        rows = cur.fetchall()

    # Group by article_id
    from collections import defaultdict

    articles: Dict[int, Dict[str, Any]] = defaultdict(
        lambda: {"article_number": None, "obligations": []}
    )

    for row in rows:
        ob_id, article_id, polarity, obligation, refs_json, article_number = row
        articles[article_id]["article_number"] = article_number
        articles[article_id]["obligations"].append(
            {"id": ob_id, "polarity": polarity, "obligation": obligation, "references": refs_json}
        )

    html_parts = [
        """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Obligations</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; }
    details { margin-bottom: 1rem; border: 1px solid #ccc; padding: 0.5rem; }
    summary { cursor: pointer; font-weight: bold; }
    .obligation { margin: 0.5rem 0; padding: 0.5rem; background: #f9f9f9; }
    .polarity { font-weight: bold; }
    .positive { color: green; }
    .negative { color: red; }
    .references { margin-top: 0.5rem; font-size: 0.9em; color: #555; }
    .ref-item { margin: 0.25rem 0; padding: 0.25rem; background: #fff; border-left: 3px solid #ddd; }
  </style>
</head>
<body>
  <h1>Obligations</h1>
"""
    ]

    for article_id in sorted(articles.keys(), key=lambda aid: articles[aid]["article_number"] or 0):
        article = articles[article_id]
        article_number = article["article_number"] or "N/A"
        obligations: list[dict[str, Any]] = article["obligations"]

        html_parts.append(
            f"""
  <details>
    <summary>Article {article_number} ({len(obligations)} obligation(s))</summary>
"""
        )

        for ob in obligations:
            polarity_class = "positive" if ob["polarity"] == "positive" else "negative"
            html_parts.append(
                f"""
    <div class="obligation">
      <div class="polarity {polarity_class}">{ob["polarity"].upper()}</div>
      <p>{ob["obligation"]}</p>
"""
            )

            # Display references if present
            refs_raw = ob.get("references")
            refs: list[dict[str, Any]] = (
                cast(list[dict[str, Any]], refs_raw) if isinstance(refs_raw, list) else []
            )
            if refs:
                html_parts.append('      <div class="references"><strong>References:</strong>\n')
                for ref in refs:
                    article_id_ref = ref.get("article_id", "")
                    text = ref.get("text", "")
                    html_parts.append(
                        f'        <div class="ref-item"><strong>{article_id_ref}:</strong> {text}</div>\n'
                    )
                html_parts.append("      </div>\n")

            html_parts.append("    </div>\n")

        html_parts.append("  </details>\n")

    html_parts.append("</body>\n</html>")
    return "".join(html_parts)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "retrieval_api.main:app", host="0.0.0.0", port=int(os.getenv("RETRIEVAL_API_PORT", "3000"))
    )
