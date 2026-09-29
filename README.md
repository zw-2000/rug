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
| M3 | AD/LDAP login, per-folder permissions, admin overrides, audit log | **done** (see "M3 status") |
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

## Sign-in, permissions, uploads (M3)

`rug serve` runs a small HTTP API (the chat endpoint and the React UI arrive in M4).

- **Sign-in.** The password is verified by binding to Active Directory *as the user* (`CORP\user`
  or `user@suffix`), so no service-account secret is stored. Empty passwords are refused before
  the directory is contacted (some directories treat them as an anonymous bind that succeeds).
  Wrong password and unknown user give the same answer. `ldap://` without StartTLS is refused
  unless `RUG_LDAP_ALLOW_INSECURE=true`. Failed sign-ins are throttled per username and per
  address (counted from the audit log; HTTP 429).
- **Sessions.** Server-side rows; the cookie is a random token signed with `RUG_SESSION_SECRET`
  (required, 32+ characters; the server will not start without it). HttpOnly, Secure,
  SameSite=Lax, 8 h absolute lifetime. State-changing requests need the session's token in
  `X-CSRF-Token` (returned by login and `GET /api/me`). An admin can revoke a user's sessions
  or disable the user; both take effect on the next request.
- **Who sees what.** `effective folders = (AD-group→folder map ∪ per-user allows) − per-user denies`,
  computed on every request; a deny always wins; nothing configured means nothing visible.
  Map edits apply immediately. **AD group membership is read at login**, so a change in AD
  applies at that user's next sign-in (or revoke their sessions).
  - The group key `*` means every signed-in user.
  - **Admins manage configuration and are granted no documents by default.** Give the admin group
    folders like any other group. This is a default, not a security boundary: an admin can map
    or allow any folder for themselves, and every such change is audited.
  - Files directly in the NAS root have the folder `""`; nobody sees them until an administrator
    maps `""`.
- **Status codes.** Download: unknown id 404, existing document you may not access 403. Upload to
  a folder you may not use: 403 (checked before anything is written). This differs on purpose
  from the question endpoint's pinned-document behaviour (M2), which gives the same answer for
  "forbidden" and "does not exist".
- **Upload** (`POST /api/upload`, multipart `folder` + `file`). Accepted types are the loader
  registry's extensions (`.docx` today), checked by extension *and* structure (a zip with
  `word/document.xml`). The size cap (50 MB) is enforced while streaming, not from
  `Content-Length`. The target folder must already exist (top-level folders are never created).
  The name is sanitised (client paths, control characters, `<>:"|?*`, reserved Windows names,
  leading `.`/`~$` are refused or reduced), an existing name gets ` (1)`, ` (2)`… and is never
  overwritten, and the file is indexed immediately. If the indexer is busy or Ollama is down the
  file is still saved (`indexed: false`) and the next scan picks it up.
- **Audit log.** Sign-ins (ok/failed/throttled), sign-outs, downloads, uploads, refused
  attempts and every administration change are written in the same transaction as the change.
  A database trigger rejects UPDATE and DELETE on the table. Passwords/tokens are never logged.

Endpoints: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/me`,
`GET /api/documents/{id}/download`, `POST /api/upload`, admin: `GET /api/admin/config`,
`PUT /api/admin/groups`, `PUT /api/admin/overrides`, `POST /api/admin/users/{name}/{revoke|disable|enable}`,
`GET /api/admin/audit`, and `GET /healthz`.

### M3 status: what is and is not verified

- Verified here (real Postgres 16 + pgvector; ldap3's **mock** directory): login rules, session
  and CSRF handling, effective-folder maths, download 200/403/404, upload rules, admin edits,
  audit append-only trigger, and a leak matrix: every golden question asked as five different
  principals (sales-only, deny beating a group grant, allow override, admin with no mapped
  group, no groups) with no excerpt, source, candidate or resolved document outside the
  principal's folders.
- **Not verified** (no domain controller in the build environment): real TLS/certificate
  validation against your directory, the exact bind behaviour and lockout policy of your AD,
  and **nested groups**: `memberOf` lists direct memberships only, so a user who is in a group
  only through another group is not matched, and the primary group (usually Domain Users) is not
  listed (map `*` for "everyone"). Test with a real account before relying on it.
- Behind a reverse proxy, set `RUG_TRUSTED_PROXIES`; otherwise every client shares the proxy's
  address and the per-address login limit becomes company-wide.
- An upload's body is parsed (and spooled to the app's temp directory) before the folder check,
  so a forbidden upload never touches the NAS but does briefly use temp space (capped at the
  upload size limit). Every other request body is capped at 64 KB.
- The leak matrix calls `Rag.ask` with folders from `effective_folders` directly; the
  session → folders → ask wiring is tested once M4 adds the chat endpoint.
- Requests run on synchronous database sessions; fine for a small team, revisit if the load grows.

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
