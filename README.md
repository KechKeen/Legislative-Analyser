Data aggregation service for legal documents aimed at LLM-enrichment workflows.

# Running locally

## System requirements
- docker
- python3

## Setup

1. In the root folder, run `make init` to initialize the development environment (one-time setup).
2. Replace required placeholders in `infra/dev/.env`.
3. In the root folder, run `make docker-up` to spin up the local environment and apply the SQL migrations.
4. Navigate to `http://localhost:8181` in your browser to view Postgres data, using the following login details:

    ```
    System: PostgreSQL
    Server: postgres
    Username: <username from .env>
    Password: <password from .env>
    Database: legal
    ```

All scripts are executed via `make run-script name=<script> [args='<args>']`.

# Workflows

## 1. Semantic Search (RAG)

Ingest legal documents, chunk them, and make them searchable via the retrieval API.

| Step | Script / Service | Args | Purpose |
|------|-----------------|------|---------|
| 1 | `pull_cellar.py` | *(none)* | Download EU-Lex legislation (FORMEX XML) or load local XML files, and insert into Postgres |
| 2 | `split_text.py` | `--asset-id <ID>` | Break asset XML into granular parts (articles, paragraphs, sub-items) |
| 3 | `chunk_parts_weaviate.py` | *(none)* | Chunk asset parts and index in Weaviate for vector search |
| 4 | Chat UI (`localhost:3000/ui`) | | Browser-based chat interface that calls the `/rag` endpoint |
| 4 (alt) | Retrieval API (`localhost:3000`) | | Query via `GET /search/chunks`, `GET /search`, or `POST /rag` |

Example:

```sh
make run-script name=pull_cellar
make run-script name=split_text args='--asset-id 1'
make run-script name=chunk_parts_weaviate
# Open http://localhost:3000/ui in a browser for the chat interface
# Or query the API directly:
curl "http://localhost:3000/search/chunks?q=data+protection"
```

## 2. Obligation Extraction & Cross-Document Overlap Analysis

Extract structured articles and binding legal obligations, then find semantic overlaps between two legal instruments. Requires `pull_cellar.py` (Workflow 1, step 1) to be completed for each asset.

| Step | Script | Args | Purpose |
|------|--------|------|---------|
| 1 | `extract_articles.py` | `--asset-id <ID>` | Parse FORMEX XML and extract individual articles |
| 2 | `extract_definitions.py` | `--asset-id <ID>` | Extract defined terms from the definitions section |
| 3 | `generate_article_references.py` | `--asset-id <ID>` | Detect internal "Article X" cross-references |
| 4 | `generate_obligations.py` | `--asset-id <ID>` | Use OpenAI to extract binding obligations from articles |
| 5 | `sync_obligations_weaviate.py` | `--asset-id <ID>` | Index obligations in Weaviate for semantic search |
| 6 | `find_obligation_overlaps.py` | `--source-asset-id <ID> --dest-asset-id <ID>` | Find semantic overlaps between two assets' obligations |

Once obligations have been extracted, you can browse them at `http://localhost:3000/obligations/ui` — a read-only viewer that lists all obligations grouped by article with collapsible sections.

Example (assuming two assets have been ingested):

```sh
make run-script name=extract_articles args='--asset-id 1'
make run-script name=extract_definitions args='--asset-id 1'
make run-script name=generate_article_references args='--asset-id 1'
make run-script name=generate_obligations args='--asset-id 1'
make run-script name=sync_obligations_weaviate args='--asset-id 1'
# repeat steps 1-5 for asset 2, then:
make run-script name=find_obligation_overlaps args='--source-asset-id 1 --dest-asset-id 2'
```

## Utility Scripts

- `check_schema.py` — Print the current database schema (diagnostic).

```sh
make run-script name=check_schema
```
