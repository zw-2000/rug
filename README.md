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
| M4 | Web API + React chat, upload, download | **done** (see "M4 status") |
| M5 | Admin UI, Docker Compose + Caddy, backups | **done** (see "M5 status") |

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

`rug serve` runs the HTTP API (chat and the React UI are described under M4).

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

## Chat, web UI, upload, download (M4)

`frontend/` is a React + Vite + TypeScript app (plain CSS). Build it and let the API serve it:

```bash
cd frontend && npm ci && npm run build          # -> frontend/dist
cd ../backend && RUG_STATIC_DIR=../frontend/dist rug serve --host 0.0.0.0 --port 8000
```

Pages: **Sign in**, **Ask** (streamed answer, "which one?" picker, pinned-document chip, source
cards with Download and the other versions, 👍/👎), **Upload** (only folders you may use that exist
on the share; accepted types and the size limit come from the server). The admin screens arrive in M5.

- **`POST /api/chat`** streams Server-Sent Events over `fetch`: `status` (`queued` while waiting for
  the model's turn, then `working`), provisional `token`s, and one final `answer`. **Only the final
  `answer` event is authoritative**: it carries the validated text (invalid citations removed, "not
  found" normalised), the sources, and the picker candidates, and it replaces the draft the UI showed
  while streaming. Searchable folders are always the caller's effective folders, never anything the
  client sends. Pinning a document you may not access gets the same "not found" reply as an unknown id.
- **One question at a time** reaches the local model (`RUG_CHAT_CONCURRENCY`, default 1); up to
  `RUG_CHAT_MAX_QUEUE` wait ("Waiting for the model…"), beyond that the API answers 503. `rug summarize`
  yields to live questions between documents; it cannot interrupt a summary already being written.
- **Answers are rendered as text only** (no HTML or Markdown rendering; `[n]` markers become
  superscripts), because model output is derived from untrusted document content. The API sends a
  `Content-Security-Policy` of `default-src 'self'` and other hardening headers.
- **Question log.** Every question is stored (user, question, resolved document, chunk ids, answer,
  latency, thumbs and comment) and readable only by admins (`GET /api/admin/qa`); rows are deleted
  after `RUG_QA_RETENTION_DAYS` (90). Tell your users that questions are logged.
- `GET /api/documents/{id}/versions` lists all versions of a document you may access.

Tests: `pytest` covers the endpoints (including the permission-leak check through HTTP: a sales-only
user asking every golden question sees no other folder's documents, filenames or text).
`cd frontend && npm test` runs unit tests (SSE parser, citation rendering) and `npm run e2e` runs
Playwright in Chromium against `backend/harness/e2e_server.py`: sign in, ask the SR-1098 question, see
the cited answer, download the file and compare its checksum with the one on disk; plus wrong
password, cross-folder access (403), the picker, feedback, sign-out and upload.

### M4 status: what is and is not verified

- Verified here: the API and UI plumbing above, in a real browser.
- **The end-to-end tests use fake models** (hash embeddings and a model that quotes the first
  excerpt), so they prove the plumbing, not answer quality. The real model's answers, its token
  streaming through Ollama, latency on your GPU and the `qwen2.5:7b-instruct-q4_K_M` tag remain
  unverified; run `rug eval --live` and try the UI on the GPU server.
- Not done: "not right? pick another" for a confidently resolved document (the picker appears only when
  the match is ambiguous; use "Keep asking about this document" / the chip to stay on a document), the
  background indexer schedule (`rug ingest` is still run by hand or cron; M5), and the admin screens.
- If a browser tab is closed mid-answer the server is meant to stop the model call at the next token
  and drop a question still queued. This is implemented but **not tested** (the test client cannot
  simulate a disconnect); check it against a real Ollama.
- npm 10.9 crashed resolving peer dependencies for the newest Vite/Vitest majors, so
  `frontend/.npmrc` sets `legacy-peer-deps=true`. Versions are pinned by `package-lock.json`.

## Deployment and administration (M5)

### Admin screens

Administrators (members of `RUG_LDAP_ADMIN_GROUP_DN`) get an **Admin** tab: **Access** (which AD
group sees which folders; per-person allow/deny exceptions), **Users** (last sign-in, sign out
everywhere, disable/enable), **Document types** (the words the resolver treats as SOW, change
request, MSA, NDA, …; an empty list means the built-in defaults), **Index** (counts, recent scans,
files that could not be read, "Scan now"), **Questions** (the question log with a thumbs-down filter and
an export shaped like `eval/golden.yaml`; also `rug export-feedback FILE`), and **Audit log**. The
tab is hidden from everyone else and every endpoint refuses non-admins on the server.

Changing the document-type vocabulary changes resolver scoring. Re-run `rug eval` after edits you care about.

### Background worker

`rug worker` (its own container) scans the share every `RUG_SCAN_INTERVAL_S` (10 min), then writes
pending summaries, yielding to live questions. An outage never crashes it: Ollama down, the share
unmounted (a scan that would delete most of the catalog is refused) or another scan running just skips
that step and it tries again next cycle.

### Deploying with Docker Compose

Needs Docker with Compose v2.24+, a Linux host, the NAS mounted on it, and for speed an NVIDIA GPU with
the [NVIDIA Container Toolkit] (the GPU part is **not verified** by the project's own tests).

```bash
cd deploy
cp .env.example .env            # fill in POSTGRES_PASSWORD, RUG_SESSION_SECRET, DOCS_DIR, RUG_HOST, LDAP_*
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build   # drop the gpu file for CPU-only
docker compose --profile setup run --rm ollama-pull                              # download the models once
```

Then open `https://<RUG_HOST>/`, sign in with an AD account from the admin group, and grant folders to
groups under **Admin → Access** (nobody sees any document until you do). Caddy uses its own certificate
authority (`tls internal`), so browsers warn until people trust its root certificate
(`docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .`) or you install a company-CA
certificate (see the comments in `deploy/Caddyfile`; add HSTS only then).

Services: `caddy` (only ports 80/443 published) → `app` (API + web UI, non-root) and `worker`,
`postgres` (pgvector), `ollama` (no published port), `backup`. Only Caddy, pinned to `172.28.0.10`, is
trusted to report client addresses (`RUG_TRUSTED_PROXIES`), so the login throttle and audit log see real
client addresses.

**NAS mount.** Mount the share read-write on the host, and make the container's user (uid 10001) able to
write to it, otherwise uploads fail with "permission denied" and nothing else notices. For CIFS:
`mount -t cifs //nas/documents /mnt/nas/documents -o credentials=/etc/rug-nas.cred,uid=10001,gid=10001,file_mode=0664,dir_mode=0775`
(NFS: export it so uid 10001 can write). Top-level folders on the share are the permission folders;
create new ones on the share itself (the app never creates them).

**Models.** `RUG_CHAT_MODEL` / `RUG_EMBED_MODEL` are pulled by `ollama-pull`. Check the chat model tag exists in
your Ollama before relying on it. Changing the embedding model needs a re-index.

### Backups

The `backup` service writes a custom-format `pg_dump` nightly (`BACKUP_AT`, local time) to `BACKUP_DIR` and
keeps the newest `BACKUP_KEEP` (14) by count. A dump is checked with `pg_restore --list` before it is
kept. **Dumps contain the full text of every indexed document plus the audit and question logs: protect the
folder like the NAS itself.** The NAS files themselves are not backed up by this; the search index can be
rebuilt from them (`rug ingest`), but users' permissions, the question log and the audit log cannot.

Restore (stop `app`, `worker`, `backup` first for an in-place restore):

```bash
docker compose run --rm --no-deps --entrypoint sh backup /restore.sh /backups/rug-YYYYMMDDTHHMMSSZ.dump rug --replace
```

`deploy/smoke.sh` restores the newest dump into a scratch database and compares row counts.

### Smoke test

`deploy/smoke.sh` builds the image and starts the real Caddy and Postgres containers with the test
harness (mock directory, fake models), then checks: verified HTTPS through Caddy, HTTP→HTTPS redirect,
the UI, cookie flags, streaming arriving incrementally through the proxy, the download checksum, client
addresses in the audit log (not Caddy's, not a spoofed header), non-root user, only Caddy publishing ports,
backup → restore (documents, chunks, audit rows, users, pgvector extension, embeddings) and retention.

### M5 status: what is and is not verified

- Verified here (Docker 29 / Compose 5 in the build environment): the smoke test above, the 14 browser
  tests (including the admin screens), 314 backend tests and the offline eval gate.
- **Not verified:** sign-in against a real Active Directory (the smoke test uses a mock directory, so the
  whole production sign-in path over TLS is untested); Ollama, the models and the GPU
  (`docker compose … gpu.yml`, the NVIDIA Container Toolkit, `ollama-pull`); **Tesseract in the image** (the
  build environment's network policy blocked Debian's package mirrors, so the smoke image was built with
  `INSTALL_TESSERACT=0`; the production build path that installs it has not been run here, and
  documents containing images need it); the CIFS/NFS mount options on a real NAS; and real answer quality.
- The image is built as `app` for production; the `smoke` target adds the test harness and must never
  be deployed.
- The GitHub Actions workflow has `frontend`, `e2e` and `deploy` jobs, but Actions has not run (billing lock).
- Known limits carried over: nested AD groups and the primary group are not read from `memberOf`
  (map `*` for "everyone"); AD group changes apply at the next sign-in.

[NVIDIA Container Toolkit]: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/

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
