# rug

**Ask questions about your company documents and get a short, cited answer plus the original file
to download. Everything runs on one server on your own network; no document content leaves it.**

> *"Boost Connect SR-1098 SOW — what is the scope of work mainly about?"*
> → *"The SOW covers … [1]" — with a **Download** button for `Boost Connect SR-1098 SOW v2 FINAL.docx`.*

- Reads the `.docx` files on your NAS (other formats can be added later).
- Signs people in with their Active Directory account and shows each person **only the folders
  they are allowed to see**.
- Uses a local language model through [Ollama](https://ollama.com); nothing is sent to a cloud service.
- Admins manage access, document types and the index from the web UI. Every change is audited.

## Contents

1. [How it works](#how-it-works)
2. [Quick start (production, Docker Compose)](#quick-start-production-docker-compose)
3. [Configuration](#configuration)
4. [Using rug](#using-rug)
5. [Operating rug](#operating-rug)
6. [Security model](#security-model)
7. [Reference](#reference)
8. [Development](#development)
9. [What is verified, and what is not](#what-is-verified-and-what-is-not)
10. [Troubleshooting](#troubleshooting)

---

## How it works

```
 browser ──HTTPS──▶ Caddy ──▶ app (API + web UI) ──▶ PostgreSQL + pgvector
                                 │  ▲                     ▲
                                 │  └── Ollama (chat + embeddings, local)
   NAS share ◀── read/write ─────┤                        │
   (your .docx files)            └── worker: scans the share, builds the search index
```

- **The NAS is the source of truth.** Each *top-level subfolder* of the share is a permission
  folder (`sales/`, `legal/`, …). Files directly in the share root belong to no folder and are
  visible to nobody until an admin maps the root (`""`).
- **The worker** scans the share (every 10 minutes by default), splits documents into passages,
  and stores them with a search index (keywords + vectors) in PostgreSQL.
- **A question** is answered in four steps: find which document is meant (by ID such as
  `SR-1098`, by document type such as *SOW*, or by name), fetch the best passages from
  the folders *that user may see*, let the local model answer from those passages only, then
  check that every citation points at a real passage. If nothing supports an answer, the reply is
  the same "couldn't find that in the documents you have access to" message every time.
- **Permissions are applied inside the database query**, before results are limited, and are
  recomputed on every request.

## Quick start (production, Docker Compose)

### What you need

| Need | Notes |
|---|---|
| A Linux server with Docker, Compose v2.24+ | Compose v5 was used in testing |
| The NAS share mounted on that server | Read-write; see [NAS mount](#nas-mount) |
| An Active Directory (LDAP) | Users sign in with their normal account |
| An NVIDIA GPU (recommended) | 6–8 GB is enough for the default 7B model. Needs the [NVIDIA Container Toolkit]. CPU-only works but is slow |
| A DNS name or fixed IP for the server | What people type in the browser (`RUG_HOST`) |

### Steps

Run everything from the `deploy/` folder.

```bash
cd deploy
cp .env.example .env
```

**1. Fill in `.env`.** The required values are:

| Setting | What to put |
|---|---|
| `POSTGRES_PASSWORD` | Letters and digits only: `openssl rand -hex 24` |
| `RUG_SESSION_SECRET` | 32+ random characters: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DOCS_DIR` | The mounted NAS folder on this server, e.g. `/mnt/nas/documents` |
| `RUG_HOST` | The name or IP people will use, e.g. `rug.corp.local` |
| `RUG_LDAP_URL`, `RUG_LDAP_BASE_DN`, `RUG_LDAP_NETBIOS_DOMAIN` | Your directory, e.g. `ldaps://dc1.corp.local`, `dc=corp,dc=local`, `CORP` |
| `RUG_LDAP_ADMIN_GROUP_DN` | The AD group whose members administer rug |

Keep `.env` private; it is git-ignored.

**2. Start the stack.**

```bash
# With an NVIDIA GPU:
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
# CPU only (slow):
docker compose up -d --build
```

> If you use the GPU file, add it (`-f docker-compose.yml -f docker-compose.gpu.yml`) to every
> `docker compose` command below.

**3. Download the models once.**

```bash
docker compose --profile setup run --rm ollama-pull
```

**4. Open `https://<RUG_HOST>/`** and sign in with an account from the admin group.
Your browser will warn about the certificate (see [Certificates](#certificates)).

**5. Grant access.** Nobody sees any document until you do. Go to **Admin → Access**, add each AD
group (its distinguished name, e.g. `CN=Sales,OU=Groups,DC=corp,DC=local`) and tick the folders
it may see. Use `*` for "every signed-in user". Being an admin grants no documents by itself.

**6. Wait for (or trigger) the first scan.** **Admin → Index** shows progress; **Scan now** starts
one immediately. Then ask a question.

### Before you rely on it

Work through this list once on the real server; several items could not be tested during development
(see [what is verified](#what-is-verified-and-what-is-not)):

- [ ] Sign in with a real AD account, and with one whose group membership is *indirect* (nested
      groups are not followed; map `*` for "everyone").
- [ ] Upload a small `.docx` through the UI and confirm it lands on the share ([NAS mount](#nas-mount)).
- [ ] Confirm the chat model tag exists in your Ollama (`ollama list`); the default is
      `qwen2.5:7b-instruct-q4_K_M`.
- [ ] Measure real answer quality with `rug eval --live` ([Evaluation](#evaluation)).
- [ ] Replace Caddy's self-signed certificate with a company-CA certificate ([Certificates](#certificates)).
- [ ] Decide who may read the backup folder ([Backups](#backups)) and tell users that
      questions are logged.

### NAS mount

Mount the share read-write on the host, and let the containers' user (uid/gid **10001**) write to it.
Otherwise uploads fail with "could not save the file to the document share".

```bash
# CIFS/SMB example
mount -t cifs //nas/documents /mnt/nas/documents \
  -o credentials=/etc/rug-nas.cred,uid=10001,gid=10001,file_mode=0664,dir_mode=0775
```

For NFS, export the share so that uid 10001 can write. Create new top-level folders **on the share**;
rug never creates them.

### Certificates

Caddy uses `tls internal` (its own certificate authority), so browsers warn until each machine trusts
its root certificate:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .
```

This is fine for a pilot. Before wider rollout, install a certificate from your company CA (see the
comments in `deploy/Caddyfile`) and only then enable HSTS.

If your domain controller's certificate is signed by a private CA, put that CA's PEM file where the `app`
container can read it (add a volume for it to `docker-compose.yml`) and set `RUG_LDAP_CA_CERTS_FILE`
to its path inside the container.

## Configuration

Settings are environment variables with the prefix `RUG_`. In Docker Compose they come from
`deploy/.env`; for local development, from `backend/.env`. The complete, commented lists are
`deploy/.env.example` and `backend/.env.example`. The ones you are most likely to touch:

| Setting | Default | Meaning |
|---|---|---|
| `RUG_DOCS_DIR` | – | Root of the NAS share (Compose sets it for you) |
| `RUG_LDAP_URL` | – | `ldaps://…`. Plain `ldap://` is refused unless `RUG_LDAP_START_TLS=true` (or `RUG_LDAP_ALLOW_INSECURE=true`, development only) |
| `RUG_LDAP_NETBIOS_DOMAIN` / `RUG_LDAP_UPN_SUFFIX` | – | How users are bound: `DOMAIN\user`, or `user@suffix` |
| `RUG_LDAP_ADMIN_GROUP_DN` | – | Members are administrators |
| `RUG_SESSION_SECRET` | – (required) | Signs session cookies. The server will not start without it |
| `RUG_SESSION_TTL_S` | 28800 | Session lifetime (8 h, absolute) |
| `RUG_CHAT_MODEL` / `RUG_EMBED_MODEL` | `qwen2.5:7b-instruct-q4_K_M` / `nomic-embed-text` | Ollama models. Changing the embedding model needs [`rug reembed`](#changing-the-embedding-model) |
| `RUG_CHAT_CONCURRENCY` / `RUG_CHAT_MAX_QUEUE` | 1 / 20 | Questions answered at once / allowed to wait (more get HTTP 503) |
| `RUG_SCAN_INTERVAL_S` | 600 | How often the worker scans the share |
| `RUG_MAX_UPLOAD_MB` | 50 | Upload size cap |
| `RUG_QA_RETENTION_DAYS` | 90 | The question log is deleted after this long |
| `RUG_TRUSTED_PROXIES` | `[]` | Reverse proxies whose `X-Forwarded-For` is believed (Compose sets Caddy's address) |
| `BACKUP_AT`, `BACKUP_KEEP`, `BACKUP_DIR`, `TZ` | 02:30, 14, `./backups`, UTC | Backup schedule (in the time zone `TZ`), copies kept, folder |

Resolver thresholds (`RUG_RESOLVER_*`) were tuned on a synthetic corpus; re-check them against your real
file names.

## Using rug

### For everyone

- **Ask.** Name the document and ask your question. Answers stream in, with `[1]`-style citations and
  source cards showing the file, the sections used, and a **Download** button. If a document has several
  versions, the newest answers, and the card lists the others.
- **"Which one?"** If your wording matches several documents, you get a short list; pick one.
  *Keep asking about this document* pins it so follow-up questions stay on it (click ✕ on the chip to stop).
- **Not found?** The answer says so instead of guessing. Answers with no verifiable citation carry a warning.
- **👍 / 👎** on an answer feed the administrators' review list.
- **Upload.** Choose one of your folders and a `.docx` (up to 50 MB). An existing file is never
  overwritten (a copy becomes `name (1).docx`). The new file is searchable within moments.

### For administrators

The **Admin** tab (visible only to admins; the server enforces it too) has:

| Section | What it does |
|---|---|
| **Access** | Which AD group sees which folders; per-person *allow* / *deny* exceptions (deny always wins) |
| **Users** | Last sign-in; sign someone out everywhere; disable/enable them in rug (not in AD) |
| **Document types** | The words that mean SOW, change request, MSA, NDA … (used to find the right document). Emptying the list restores the built-in defaults |
| **Index** | Document counts, recent scans, files that could not be read, **Scan now** |
| **Questions** | Every question asked, with answers and ratings; filter to 👎; export them as a starting point for new test questions |
| **Audit log** | Sign-ins, downloads, uploads, refusals and every admin change; cannot be edited or deleted |

Effective access for a person is *(folders of their AD groups + their allow exceptions) − their deny
exceptions*. Edits apply on the person's next request. Changes to someone's AD groups apply the next
time they sign in (or use **Sign out everywhere**).

## Operating rug

Commands run from `deploy/`.

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f app worker` |
| Update rug | `git pull && docker compose up -d --build` |
| Stop / start | `docker compose stop` / `docker compose start` |
| Force a scan | Admin → Index → **Scan now** |
| Index statistics | `docker compose exec app rug stats` |
| Ask from the terminal | `docker compose exec app rug ask "…"` (unrestricted view; add `--folders sales,legal` to restrict) |

### Adding folders and documents

Add a top-level folder or files on the NAS itself. The worker picks them up on its next scan. Then
grant the folder to a group under **Admin → Access**. Notes on what gets indexed:

- Only `.docx` today. Hidden files, Office `~$` lock files and zero-byte files are ignored.
- Renamed or moved files are re-tagged without re-reading; removed files disappear from the index.
- A scan that would delete most of the catalog (for instance because the share is not mounted) is
  refused and logged; nothing is deleted.
- Versions (`v2`, `FINAL`, `(1)`, dates, `copy of` …) of one document within a folder are grouped; the newest
  by modification time answers, and a tie makes rug ask.

### Backups

The `backup` service writes a `pg_dump` nightly to `BACKUP_DIR` (default `deploy/backups`) at `BACKUP_AT`
in the time zone `TZ`, checks that it is readable, and keeps the newest `BACKUP_KEEP` files.

> **Dumps contain the full text of every indexed document plus the audit and question logs.** They are
> written owner-only; keep the folder as protected as the NAS.

The NAS files themselves are **not** backed up by rug. The search index can be rebuilt from them
(`rug ingest`), but users' permissions, the question log and the audit log cannot; that is what the dump is for.

**Take one now:** `docker compose exec backup sh /backup.sh once`

**Restore in place** (stop `app`, `worker` and `backup` first):

```bash
docker compose run --rm --no-deps --entrypoint sh backup \
  /restore.sh /backups/rug-YYYYMMDDTHHMMSSZ.dump rug --replace
docker compose start app worker backup
```

Without `--replace` the target database must not exist, so you can restore into a scratch database
and inspect it first.

### Changing the embedding model

Vectors from different models are not comparable, so rug records which model built the index and
**refuses to start** (`serve`, `worker`, `ingest`, `summarize`) if `RUG_EMBED_MODEL` or its prefixes no
longer match, naming the fix:

1. `docker compose stop app worker backup`
2. Set the new `RUG_EMBED_MODEL` in `.env`, then `docker compose --profile setup run --rm ollama-pull`
3. `docker compose run --rm --no-deps app rug reembed` (slow on a large corpus; if interrupted the index stays
   marked unusable, so just run it again)
4. `docker compose start app worker backup`

A model with a different vector size also needs a schema change. An index created before this check
existed is assumed to match the current setting (a warning is logged).

### Host settings worth knowing

- The stack uses the Docker network `172.28.0.0/24` (Caddy is pinned to `172.28.0.10`, and only Caddy is trusted
  to report client addresses). Change both in `docker-compose.yml` if your LAN already uses that range.
- Only Caddy publishes ports (80, 443). Postgres and Ollama are reachable only inside the Docker network.
- `deploy/smoke.sh` is a self-test of the deployment files (see [Development](#development)); it does not
  touch your real stack or data.

## Security model

- **Sign-in** checks the password by binding to AD *as the user*: no service-account password is stored.
  Empty passwords are refused before the directory is contacted; wrong password and unknown user give the same
  answer; failed sign-ins are throttled per account and per address (HTTP 429).
- **Sessions** are server-side, with a signed, `HttpOnly`, `Secure`, `SameSite=Lax` cookie (8 h) and a CSRF
  token required on every state-changing request.
- **Scope first.** Every search takes the caller's folders as a required argument, applied in SQL before any
  `LIMIT`. Empty means nothing. Only the CLI (`rug ask`) has an "all folders" view.
- **Status codes.** Download of an unknown id is 404; of an existing document you may not access, 403.
  Uploading to a forbidden folder is 403 (checked before anything is written). Asking a question about a
  document you may not access gets the same "not found" reply as an unknown one.
- **Uploads** are checked by extension and by structure, capped while streaming, sanitised (no paths, control
  characters or reserved names), never overwrite, and can only go into existing top-level folders.
- **Model output is untrusted.** The model sees only numbered excerpts, invalid citations are removed, and the
  UI renders answers as plain text (no HTML/Markdown), under a strict Content-Security-Policy.
- **Audit log** entries are written in the same transaction as the change, and a database trigger rejects
  `UPDATE`/`DELETE`. Passwords and tokens are never logged.
- **Admins** manage configuration and get no documents by default. That is a default, not a boundary: an admin
  can grant themselves a folder, and the change is audited.
- **Questions are logged** (who, what, answer, rating) for admins, and deleted after `RUG_QA_RETENTION_DAYS`.

## Reference

### API

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/me` | Session; `me` returns folders and the CSRF token |
| `POST /api/chat` | Ask; streams Server-Sent Events: `status`, provisional `token`s, then one authoritative `answer` (or `error`) |
| `POST /api/qa/{id}/feedback` | 👍/👎 on your own answer |
| `GET /api/documents/{id}/download`, `GET /api/documents/{id}/versions` | Original file; all versions |
| `POST /api/upload` | Multipart `folder` + `file` |
| `GET /api/admin/config`, `PUT /api/admin/groups`, `PUT /api/admin/overrides` | Access |
| `GET /api/admin/users`, `POST /api/admin/users/{name}/{revoke\|disable\|enable}` | Users |
| `GET/PUT /api/admin/doc-types`, `DELETE /api/admin/doc-types/{name}` | Document types |
| `GET /api/admin/index`, `POST /api/admin/index/scan` | Index status; scan now |
| `GET /api/admin/qa`, `GET /api/admin/qa/export`, `GET /api/admin/audit` | Logs |
| `GET /healthz` | Health check |

### Command line (`rug …`)

| Command | Purpose |
|---|---|
| `migrate` | Apply database migrations (the container does this on start) |
| `serve` / `worker` | The API + web UI / the background scanner and summariser |
| `ingest` | One scan of the share now |
| `summarize` | Write document overviews (slow; yields to live questions) |
| `reembed` | Rebuild all vectors after changing the embedding model |
| `ask "…"` | Ask from the terminal |
| `stats` | Documents per folder |
| `eval` | Score the golden questions (see below) |
| `gen-synthetic DIR` | Write a fictional test corpus |
| `export-feedback FILE` | Logged questions → skeleton for new test questions |

### How answering works, in detail

1. **Resolve the document** among the newest version of each document in the caller's scope: document
   IDs (`SR-1098`, `sr1098`, `SR 1098`), document-type words (SOW, change request, MSA, …), then
   typo-tolerant, IDF-weighted name matching that must hold in both directions. One clear winner → answer from it;
   several close → ask which; no document named → search the whole scope; an ID from a series the scope uses but no
   visible document carries → "not found" (never answered from a different document).
2. **Retrieve** with Postgres keyword search plus vector search, fused by reciprocal-rank fusion. Vector search is
   exact for scopes up to `RUG_EXACT_SCAN_MAX_CHUNKS` chunks and otherwise uses the HNSW index, repeating exactly if
   the index under-delivers (filtered HNSW queries can return too few rows).
3. **Answer** from numbered excerpts within a character budget that fits `RUG_CHAT_NUM_CTX`, plus the stored
   overview for overview-style questions. Citations that point nowhere are removed; an answer left with none is flagged.
4. **Overviews** (`rug summarize`, also run by the worker) are keyed by content hash, so copies and renames share them.

### What gets extracted from a `.docx`

Body paragraphs under their heading path; tables (one `Header: value; …` line per row); tracked changes as
accepted (insertions kept, deletions dropped); OCR text of embedded images at least 100×100 px (Tesseract);
comments, headers and footers are skipped. English only.

### Evaluation

`backend/eval/golden.yaml` holds 55 questions over a synthetic corpus (named documents, find-the-document,
unanswerable, permission cases).

- `rug eval --offline` (also in CI) checks the deterministic parts with fake models: right document, key facts in
  the excerpts, deterministic not-found, and **0 permission leaks**.
- `rug eval --live` on the GPU server adds citation support, answer facts and not-found on content. Set
  `RUG_EVAL_DATABASE_URL` to a **throwaway** database whose name ends in `_eval` or `_test`; it is wiped.
  With the Compose stack running (the offline eval was run locally; this Compose command line itself has not been run):

  ```bash
  docker compose exec postgres createdb -U rug rug_eval
  docker compose run --rm --no-deps app sh -c \
    'RUG_EVAL_DATABASE_URL="${RUG_DATABASE_URL%/*}/rug_eval" rug eval --live'
  ```
- The resolver was tuned on this synthetic set, so 100% offline scores are a regression baseline, not a forecast.
  Use **Admin → Questions → Export** to turn real 👎 answers into new test questions.

## Development

### Repository layout

```
backend/    Python 3.11 service (FastAPI, SQLAlchemy, Alembic)
  rug/        the application: api/, auth/, loaders/, migrations/, indexer, resolver, search, rag, worker …
  eval/       golden questions, synthetic corpus, fake models
  harness/    test-only: mock Active Directory and the server used by browser tests (never shipped)
  tests/      pytest suite
frontend/   React + Vite + TypeScript UI (plain CSS), vitest unit tests, Playwright browser tests
deploy/     Dockerfile, Compose files, Caddyfile, backup scripts, smoke test
```

Adding a document format means adding one loader to `backend/rug/loaders/`. Its extensions then become both
indexable and uploadable.

### Run it locally

Needs Python 3.11+, PostgreSQL 16 with pgvector, Tesseract, Node 22, and Ollama for real models.

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                     # then edit

docker run -d --name rug-pg -p 5432:5432 -e POSTGRES_USER=rug -e POSTGRES_PASSWORD=rug \
  -e POSTGRES_DB=rug pgvector/pgvector:pg16
docker exec rug-pg createdb -U rug rug_test

ollama pull nomic-embed-text
ollama pull qwen2.5:7b-instruct-q4_K_M   # check the tag exists in your Ollama

rug migrate
rug gen-synthetic ./sample-docs          # fictional documents
rug ingest --docs-dir ./sample-docs
rug ask "Boost Connect SR-1098 SOW - what is the scope of work mainly about?"

# the web app: build the UI, then serve it (needs RUG_SESSION_SECRET and the RUG_LDAP_* settings)
(cd ../frontend && npm ci && npm run build)
RUG_STATIC_DIR=../frontend/dist rug serve
```

### Tests

```bash
# backend (uses the rug_test database and fake models; no Ollama needed)
cd backend
export RUG_TEST_DATABASE_URL=postgresql+psycopg://rug:rug@localhost:5432/rug_test
ruff check . && ruff format --check . && mypy && pytest -q

# frontend
cd frontend
npm run typecheck && npm test && npm run build
npm run e2e        # Playwright; needs the built UI, a `rug_e2e` database, and Chromium
                   # (see frontend/playwright.config.ts; set RUG_PYTHON to your venv's python)

# deployment files: builds the image, runs real Caddy + Postgres, checks TLS, streaming,
# cookies, client addresses, checksums, backup and restore
deploy/smoke.sh
```

The browser and smoke tests run against a **mock directory and fake models**: they prove the wiring, not answer
quality or real sign-in. `frontend/.npmrc` sets `legacy-peer-deps=true` because npm 10.9 crashed resolving peer
dependencies for the newest Vite/Vitest; `package-lock.json` pins the versions.

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs the backend, frontend, e2e and deploy jobs.

## What is verified, and what is not

**Verified during development** (real PostgreSQL 16 + pgvector, real Docker, real Chromium):
permission enforcement (including a leak matrix of every golden question across five kinds of user, through the
HTTP API); resolver, retrieval and citation handling; sign-in rules, sessions and CSRF against a *mock* directory;
uploads and downloads; the admin screens; the worker's outage handling; the embedding-model guard; and the
deployment stack's TLS, streaming through Caddy, cookies, client addresses, checksums, non-root containers, and
backup → restore.

**Not verified — check these on your own server:**

- **Sign-in against a real Active Directory**, including TLS/certificate validation, your lockout policy, and
  **nested groups**. Only direct `memberOf` entries are read, and the primary group (usually *Domain Users*) is not
  listed; map `*` for "everyone". Group changes in AD apply at the person's next sign-in.
- **Ollama, the models and the GPU** (NVIDIA Container Toolkit, `docker-compose.gpu.yml`, `ollama-pull`), including
  whether the chat model tag exists, streaming through Ollama, and latency.
- **Real answer quality.** Answer, citation and content-not-found scores are unmeasured until you run
  `rug eval --live`.
- **Tesseract in the production image.** The build environment blocked Debian's package mirrors, so tests used an
  image built with `INSTALL_TESSERACT=0`. The default build (which installs it) has not been run, and documents
  containing images need it.
- **The NAS mount** options on your real share, and the backup scheduler's actual nightly wake-up (its next-run
  arithmetic and a manual backup are tested).
- **Browser tab closed mid-answer:** the server is meant to stop the model call; implemented but untested.
- **GitHub Actions** has not run yet (the account was billing-locked); the same checks were run locally.

**Known limits:** `.docx` only; English only; one question at a time reaches the model; "not right? pick another"
is offered only when the match is ambiguous (use the pinned-document chip otherwise); requests use synchronous
database sessions, which is fine for a small team.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `app` keeps restarting; log says `RUG_SESSION_SECRET must be set…` | Set a 32+ character `RUG_SESSION_SECRET` in `.env`. |
| `app`/`worker` stop with "the index was built with embedding model …" | `RUG_EMBED_MODEL` changed. Restore it, or follow [Changing the embedding model](#changing-the-embedding-model). |
| "The sign-in service is unavailable" (503) | The directory cannot be reached: check `RUG_LDAP_URL`, DNS/firewall from the server, and that the domain controller's CA is trusted (`RUG_LDAP_CA_CERTS_FILE`). |
| "Invalid username or password" for a correct login | Check `RUG_LDAP_NETBIOS_DOMAIN` (or `RUG_LDAP_UPN_SUFFIX`) and `RUG_LDAP_BASE_DN`; the account must be found under that base. The password itself is never logged. |
| "Too many failed attempts" (429) | Wait for the window (10 min). If everyone hits it, the proxy address is being treated as the client: check `RUG_TRUSTED_PROXIES`. |
| Signed in, but "You don't have access to any document folders" | An admin has not mapped one of your AD groups to a folder (**Admin → Access**), or your group changed in AD: sign out and in again. |
| A document does not appear in answers | Is it a `.docx` in a top-level folder you can see? Check **Admin → Index** for "could not be read" and the scan history, wait for the next scan, or **Scan now**. Files in the share root are visible to nobody unless the root is mapped. |
| Upload fails: "could not save the file to the document share" | The share is read-only or not writable by uid/gid 10001: see [NAS mount](#nas-mount). |
| "Waiting for the model…" | Questions are answered one at a time; others wait. Raise `RUG_CHAT_CONCURRENCY` only if your GPU and `OLLAMA_NUM_PARALLEL` can take it. |
| Worker log: "scan refused: … would delete" | The share looks empty or unmounted. Fix the mount. If the deletion is intended, run `rug ingest --allow-mass-delete`. |
| Worker log: "embeddings unavailable" / "model unavailable" | Ollama is down or the models are not pulled: `docker compose --profile setup run --rm ollama-pull`. The worker retries every cycle. |
| Browser certificate warning | Expected with Caddy's internal CA: trust its root certificate, or install a company-CA certificate ([Certificates](#certificates)). |

[NVIDIA Container Toolkit]: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/
