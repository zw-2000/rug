"""Test-only server for the browser (Playwright) tests: the real API and the built frontend, with
a mock Active Directory, the synthetic corpus, and FAKE models (hash embeddings and an extractive
"chat" that quotes the first excerpt). It proves the plumbing end to end, not answer quality.

    RUG_E2E_DATABASE_URL=postgresql+psycopg://rug:rug@localhost:5432/rug_e2e \\
      python -m harness.e2e_server --port 8765 --static ../frontend/dist

The database is wiped on start, so its name must end in _e2e or _test.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import docx
import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from eval.fakes import ExtractiveChat, HashEmbedder
from eval.synthetic_gen import build
from harness.ldapmock import MockDirectory
from rug import perms
from rug.api.app import create_app
from rug.auth.ldap import LdapAuthenticator
from rug.cli import alembic_upgrade
from rug.config import Settings
from rug.db.session import get_engine
from rug.indexer import Indexer

SALES = "CN=Sales,OU=Groups,DC=corp,DC=local"
LEGAL = "CN=Legal,OU=Groups,DC=corp,DC=local"
ADMINS = "CN=RugAdmins,OU=Groups,DC=corp,DC=local"
USERS = {
    "ann": ("ann-pass", "Ann Sales", [SALES]),
    "lee": ("lee-pass", "Lee Legal", [LEGAL]),
    "root": ("root-pass", "Root Admin", [ADMINS]),
}


class SlowChat(ExtractiveChat):
    """The extractive fake, but streamed word by word with a pause between words."""

    def __init__(self, delay: float):
        self.delay = delay

    def chat(self, messages, on_token=None):  # type: ignore[no-untyped-def]
        words: list[str] = []
        text = super().chat(messages, None)
        for w in text.split(" "):
            words.append(w)
            if on_token:
                on_token(w + " ")
                time.sleep(self.delay)
        return " ".join(words)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--token-delay",
        type=float,
        default=0.0,
        help="Seconds between streamed words of the fake model (to test that nothing buffers)",
    )
    ap.add_argument(
        "--secure-cookies", action="store_true", help="Secure cookies (when behind TLS, e.g. Caddy)"
    )
    ap.add_argument("--static", default="")
    ap.add_argument("--docs", default="", help="Folder to build the corpus in (emptied first)")
    args = ap.parse_args()

    url = os.environ.get("RUG_E2E_DATABASE_URL", "")
    name = make_url(url).database or ""
    if not name.endswith(("_e2e", "_test")):
        sys.exit("RUG_E2E_DATABASE_URL must name a throwaway database ending in _e2e or _test")

    if args.docs:
        docs = Path(args.docs).resolve()
        marker = docs / ".rug-e2e"
        if docs.exists() and not marker.exists() and any(docs.iterdir()):
            sys.exit(f"refusing to empty {docs}: it is not a folder this script created")
        shutil.rmtree(docs, ignore_errors=True)
        docs.mkdir(parents=True)
        marker.write_text("throwaway corpus for browser tests\n")
    else:
        docs = Path(tempfile.mkdtemp(prefix="rug-e2e-"))
    build(docs)
    # Two visible documents with the same name, so the "which one?" picker can be exercised.
    for folder in ("sales", "delivery"):
        d = docx.Document()
        d.add_paragraph(f"Scope of the Orion project in {folder}: install gadgets.")
        d.save(str(docs / folder / "Orion SOW SR-5000.docx"))
    settings = Settings(
        _env_file=None,
        database_url=url,
        docs_dir=docs,
        session_secret="e2e-" + "x" * 40,
        ldap_url="ldaps://mock.invalid",
        ldap_base_dn="dc=corp,dc=local",
        ldap_netbios_domain="CORP",
        ldap_admin_group_dn=ADMINS,
        cookie_secure=args.secure_cookies,  # off for plain http on localhost
        static_dir=args.static,
    )
    engine = get_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                "DROP TABLE IF EXISTS chunks, documents, document_summaries, index_runs, users, "
                "sessions, group_folders, user_overrides, audit_log, qa_log, doc_types, "
                "alembic_version"
            )
        )
    alembic_upgrade(url)
    factory = sessionmaker(engine, expire_on_commit=False)
    embedder = HashEmbedder()
    with factory() as db:
        Indexer(db, embedder, docs, settings).run()
        perms.set_group_folders(db, "e2e", SALES, ["sales", "delivery"])
        perms.set_group_folders(db, "e2e", LEGAL, ["legal"])
        db.commit()

    directory = MockDirectory(USERS)
    app = create_app(
        settings,
        session_factory=factory,
        authenticator=LdapAuthenticator(settings, directory),
        embedder=embedder,
        chat_model=SlowChat(args.token_delay) if args.token_delay else ExtractiveChat(),
    )
    print(f"E2E server: docs in {docs}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
