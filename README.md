# rug

Local, team-hosted document Q&A. Ask about a document on the company NAS, for example
*"Boost Connect SR-1098 SOW — what is the scope of work mainly about?"*, and get a short,
cited answer plus the original file to download. Runs entirely on one LAN server with a
local LLM (Ollama); no document content leaves the network.

## Status

Built in milestones, each behind a validation gate:

| Milestone | Scope | Status |
|---|---|---|
| M1 | Postgres schema, `.docx` loader (tables, tracked changes, OCR), versions, indexer | **done** |
| M2 | Resolver, hybrid retrieval, summaries, RAG, `rug ask` / `rug eval` | next |
| M3 | AD/LDAP login, per-folder permissions, admin overrides, audit log | |
| M4 | Web API + React chat, upload, download | |
| M5 | Admin UI, Docker Compose + Caddy, backups | |

## How indexing works (M1)

- `RUG_DOCS_DIR` is the NAS root. Each **top-level subfolder is a permission folder**
  (`sales/`, `legal/`, …).
- `rug ingest` walks the folder and syncs it into PostgreSQL:
  - unchanged size and mtime → skipped without reading the file
  - content changed → re-indexed
  - renamed or moved → re-tagged (path and folder updated) without re-embedding
  - copied → chunks cloned without re-embedding
  - removed from disk → deleted
  - broken file → recorded with `status=error`, retried when it changes
  - hidden files and Office `~$` lock files → ignored
- **.docx extraction:**
  - body paragraphs under their heading path
  - tables, one `Header: value; …` line per row
  - tracked changes read as accepted: insertions kept, deletions dropped
  - OCR text of embedded images (Tesseract, images ≥100×100 px)
  - comments, headers and footers skipped
- **Versions:** `v2`, `FINAL`, `(1)`, dates, `copy of` and similar markers are stripped to
  group versions of a document within a folder. The newest version answers.
- **Search index:** each chunk stores a pgvector embedding (HNSW index) and a generated
  `tsvector`. The `tsvector` combines English stemming with a `simple` copy, so IDs like
  `SR-1098` match exactly.
- **Adding a format** means adding one loader to `rug/loaders/`. Its extensions then become
  both indexable and uploadable.

## Development setup

Requirements: Python 3.11+, PostgreSQL 16 with pgvector, Tesseract (`tesseract-ocr`), and
Ollama for real embeddings.

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # edit DB URL / docs dir

# Postgres with pgvector, e.g. via Docker:
docker run -d --name rug-pg -p 5432:5432 -e POSTGRES_USER=rug -e POSTGRES_PASSWORD=rug \
  -e POSTGRES_DB=rug pgvector/pgvector:pg16
docker exec rug-pg createdb -U rug rug_test

ollama pull nomic-embed-text

rug migrate
rug gen-synthetic ./sample-docs   # fictional test corpus
rug ingest --docs-dir ./sample-docs
rug stats
```

Tests need the `rug_test` database, set with `RUG_TEST_DATABASE_URL`. They use a fake
embedder, so Ollama is not required:

```bash
ruff check . && ruff format --check . && mypy && pytest -q
```
