import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from rug.config import get_settings
from rug.db import Chunk, Document, make_session

if TYPE_CHECKING:
    from rug.rag import Answer

app = typer.Typer(no_args_is_help=True, help="rug: local document Q&A")

MIGRATIONS = Path(__file__).resolve().parent / "migrations"  # shipped inside the package


def alembic_upgrade(url: str | None = None) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    if url:
        cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    alembic_upgrade()
    typer.echo("database is at head")


@app.command()
def ingest(
    docs_dir: Path = typer.Option(None, help="Override RUG_DOCS_DIR"),
    allow_mass_delete: bool = typer.Option(
        False, help="Allow a scan to delete more than RUG_MAX_DELETE_FRACTION of the catalog"
    ),
) -> None:
    """Scan the docs folder once and update the index."""
    from rug.indexer import Indexer, MassDeletionRefused
    from rug.llm import EmbeddingError, OllamaEmbedder

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    embedder = OllamaEmbedder()
    try:
        embedder.check()
    except EmbeddingError as e:
        typer.echo(f"ERROR {e}", err=True)
        raise typer.Exit(1) from e
    with make_session() as db:
        indexer = Indexer(
            db, embedder, docs_dir or get_settings().docs_dir, allow_mass_delete=allow_mass_delete
        )
        try:
            run = indexer.run()
        except MassDeletionRefused as e:
            typer.echo(f"ERROR {e}", err=True)
            raise typer.Exit(1) from e
        typer.echo(json.dumps({"run": run.id, "files": run.total, **run.counts}))
        for err in run.errors:
            typer.echo(f"ERROR {err['path']}: {err['error']}", err=True)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Address to listen on"),
    port: int = typer.Option(8000, help="Port to listen on"),
) -> None:
    """Run the HTTP API (sign-in, download, upload, admin). Needs RUG_SESSION_SECRET and LDAP."""
    import uvicorn

    from rug.api.app import create_app
    from rug.auth.ldap import DirectoryUnavailable
    from rug.auth.sessions import ConfigError

    try:
        application = create_app()
    except (ConfigError, DirectoryUnavailable) as e:
        typer.echo(f"ERROR {e}", err=True)
        raise typer.Exit(1) from e
    uvicorn.run(application, host=host, port=port)


@app.command()
def stats() -> None:
    """Show catalog counts per folder."""
    with make_session() as db:
        rows = db.execute(
            select(Document.folder, func.count(Document.id), func.count(Document.error))
            .group_by(Document.folder)
            .order_by(Document.folder)
        ).all()
        chunks = db.scalar(select(func.count(Chunk.id)))
    for folder, n, warn in rows:
        typer.echo(f"{folder or '(root)':20} {n:6} docs  {warn:4} with errors/warnings")
    typer.echo(f"{'chunks':20} {chunks:6}")


def format_answer(answer: "Answer") -> str:
    """Terminal rendering of a validated answer: the text once, then each source labelled with
    the excerpt numbers the text cites (not with its position in the list)."""
    lines = [answer.text]
    lines += [f"  ? {c.doc.folder}/{c.doc.filename}  (id {c.doc.id})" for c in answer.candidates]
    if answer.sources:
        lines.append("")
    for src in answer.sources:
        labels = ", ".join(str(n) for n in src.refs)
        sections = "; ".join(src.sections) or "-"
        lines.append(f"[{labels}] {src.path}  ({src.n_versions} version(s))  sections: {sections}")
    if answer.note:
        lines.append(f"note: {answer.note}")
    return "\n".join(lines)


@app.command()
def summarize(
    limit: int = typer.Option(None, help="Summarise at most this many documents"),
) -> None:
    """Write overviews for documents that lack one (slow: run when the server is idle)."""
    from rug.llm import ChatError, EmbeddingError, OllamaChat, OllamaEmbedder
    from rug.summaries import live_questions_active, summarize_pending

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    chat, embedder = OllamaChat(), OllamaEmbedder()
    try:
        chat.check()
        embedder.check()
        with make_session() as db:
            counts = summarize_pending(
                db, chat, embedder, limit=limit, yield_to=lambda: live_questions_active(db)
            )
    except (ChatError, EmbeddingError) as e:
        typer.echo(f"ERROR {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(json.dumps(dict(counts)))


@app.command()
def ask(
    question: str,
    folders: str = typer.Option(None, help="Comma-separated folders; default: all folders"),
    doc: str = typer.Option(None, help="Pin the answer to one document id"),
) -> None:
    """Ask a question from the terminal (a trusted, unrestricted view unless --folders is set)."""
    import uuid

    from rug.llm import ChatError, EmbeddingError, OllamaChat, OllamaEmbedder
    from rug.rag import DocumentNotAvailable, QuestionTooLong, Rag
    from rug.scope import all_folders

    chat, embedder = OllamaChat(), OllamaEmbedder()
    try:
        chat.check()
        embedder.check()
    except (ChatError, EmbeddingError) as e:
        typer.echo(f"ERROR {e}", err=True)
        raise typer.Exit(1) from e
    with make_session() as db:
        allowed = frozenset(folders.split(",")) if folders else all_folders(db)
        try:
            # No token streaming here: the model's raw output is only shown once it has been
            # validated (citations checked, not-found normalised, stream known to be complete).
            answer = Rag(db, embedder, chat).ask(
                question, folders=allowed, pinned=uuid.UUID(doc) if doc else None
            )
        except DocumentNotAvailable:
            typer.echo("ERROR document not found", err=True)
            raise typer.Exit(1) from None
        except (ChatError, EmbeddingError, QuestionTooLong) as e:
            typer.echo(f"ERROR {e}", err=True)
            raise typer.Exit(1) from e
        typer.echo(format_answer(answer))


@app.command("eval")
def eval_cmd(
    database_url: str = typer.Option(..., envvar="RUG_EVAL_DATABASE_URL", help="Throwaway DB"),
    live: bool = typer.Option(
        False, "--live/--offline", help="Use Ollama (else deterministic fakes)"
    ),
) -> None:
    """Index the synthetic corpus into a throwaway database and score the golden questions."""
    import tempfile

    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from eval.fakes import ExtractiveChat, HashEmbedder
    from eval.runner import run_eval
    from eval.synthetic_gen import build
    from rug.indexer import Indexer
    from rug.llm import ChatError, EmbeddingError, OllamaChat, OllamaEmbedder
    from rug.summaries import summarize_pending

    name = make_url(database_url).database or ""
    if not name.endswith(("_eval", "_test")):
        typer.echo(
            f"ERROR refusing to wipe database {name!r}: its name must end in _eval or _test",
            err=True,
        )
        raise typer.Exit(2)

    embedder: Any = HashEmbedder()
    chat: Any = ExtractiveChat()
    if live:
        embedder, chat = OllamaEmbedder(), OllamaChat()
        try:
            chat.check()
            embedder.check()
        except (ChatError, EmbeddingError) as e:
            typer.echo(f"ERROR {e}", err=True)
            raise typer.Exit(1) from e

    alembic_upgrade(database_url)
    with make_session(database_url) as db, tempfile.TemporaryDirectory() as tmp:
        db.execute(
            text(
                "TRUNCATE chunks, documents, document_summaries, index_runs "
                "RESTART IDENTITY CASCADE"
            )
        )
        db.commit()
        build(Path(tmp))
        run = Indexer(db, embedder, Path(tmp)).run()
        typer.echo(f"indexed: {dict(run.counts)}")
        if live:
            typer.echo(f"summaries: {dict(summarize_pending(db, chat, embedder))}")
        report = run_eval(db, embedder, chat, live=live)
    typer.echo(report.format())
    failed = report.gate()
    typer.echo("\n" + ("GATE PASSED" if not failed else f"GATE FAILED: {', '.join(failed)}"))
    if not live:
        typer.echo(
            "(offline: model-dependent metrics were not judged; run with --live on the GPU server)"
        )
    raise typer.Exit(1 if failed else 0)


@app.command("gen-synthetic")
def gen_synthetic(out: Path = typer.Argument(..., help="Output folder")) -> None:
    """Write the synthetic evaluation corpus."""
    from eval.synthetic_gen import build

    for p in build(out):
        typer.echo(p)


if __name__ == "__main__":
    app()
