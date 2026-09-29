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
| M2 | Resolver, hybrid retrieval, summaries, RAG, `rug ask` / `rug eval` | **done** (see "M2 status") |
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

## How answering works (M2)

`rug ask "Boost Connect SR-1098 SOW — what is the scope of work mainly about?"`

1. **Scope first.** Every entry point takes the caller's `folders` as a required argument with
   no default; the filter runs inside each SQL query, before any `LIMIT`. (Users and groups
   arrive in M3; the CLI is a trusted, unrestricted view unless you pass `--folders`.)
2. **Resolve the document.** Over the newest version of each document in scope: document IDs
   (`SR-1098`, `sr1098`, `SR 1098`), typed document-type words (SOW, change request, MSA, …),
   then typo-tolerant, IDF-weighted name coverage that must hold in both directions. Outcomes:
   one clear winner → answer from it; several close → ask which; no document named → search
   the whole scope; a named ID from a series this scope uses but no visible document carries
   it → "not found" (never answered from a different document).
3. **Retrieve.** Postgres keyword search (OR-combined lexemes, never raw user text in a
   `tsquery`) plus vector search, fused with reciprocal-rank fusion. Vector search is exact
   for scopes up to `RUG_EXACT_SCAN_MAX_CHUNKS` chunks and otherwise uses the HNSW index with
   a high `ef_search`, repeating exactly if the index under-delivers (filtered HNSW queries
   can return too few rows; measured on pgvector 0.6).
4. **Answer.** The model sees only numbered excerpts (within a character budget that fits
   `RUG_CHAT_NUM_CTX`), plus the stored overview for overview-style questions. Citations that
   do not point at a real excerpt are removed; an answer with no valid citation is flagged
   `ungrounded`; every not-found path returns the same text.
5. **Overviews.** `rug summarize` writes map-reduce summaries (keyed by content hash) as a
   separate, slow pass. Run it when the server is idle: live-question priority arrives with
   the API milestone.

### Evaluation

`eval/golden.yaml` holds 55 questions over the synthetic corpus (named documents, find-the-doc,
unanswerable, permission cases).

- `rug eval --offline` (and CI): deterministic metrics only — right document, key facts
  present in the excerpts, deterministic not-found, **0 leaks**. Uses fake models.
- `rug eval --live` (GPU server): adds citation support, answer facts and not-found on
  content. `RUG_EVAL_DATABASE_URL` must name a throwaway database ending in `_eval`/`_test`;
  it is wiped.

### M2 status: what is and is not verified

- Verified here (137 tests, real Postgres 16 + pgvector 0.6): scope enforcement, resolver
  behaviour, hybrid retrieval including the exact/index paths, chat-stream handling,
  citation validation, the offline evaluation gate.
- **Not verified** (no Ollama/GPU in the build environment): real-model answer quality
  (`answer` ≥ 80%, `citation` ≥ 90%, content not-found ≥ 90% are unmeasured), the
  `qwen2.5:7b-instruct-q4_K_M` tag, whether Ollama truncates prompts beyond `num_ctx`
  (`num_ctx` is set explicitly and the prompt is budgeted to fit), the `/api/show` preflight
  against a live server, and real embedding quality.
- The resolver's thresholds were tuned on this synthetic set; treat the 100% offline scores
  as a regression baseline, not a forecast. Re-check on real filenames.

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

ollama pull qwen2.5:7b-instruct-q4_K_M   # check the tag exists in your Ollama

rug migrate
rug gen-synthetic ./sample-docs   # fictional test corpus
rug ingest --docs-dir ./sample-docs
rug stats
rug summarize                     # optional, slow: overviews for overview-style questions
rug ask "Boost Connect SR-1098 SOW - what is the scope of work mainly about?"
```

Tests need the `rug_test` database, set with `RUG_TEST_DATABASE_URL`. They use a fake
embedder, so Ollama is not required:

```bash
ruff check . && ruff format --check . && mypy && pytest -q
```
