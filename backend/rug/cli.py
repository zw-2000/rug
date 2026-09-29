import json
import logging
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from rug.config import get_settings
from rug.db import Chunk, Document, make_session

app = typer.Typer(no_args_is_help=True, help="rug: local document Q&A")

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def alembic_upgrade(url: str | None = None) -> None:
    cfg = Config(str(ALEMBIC_INI))
    if url:
        cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    alembic_upgrade()
    typer.echo("database is at head")


@app.command()
def ingest(docs_dir: Path = typer.Option(None, help="Override RUG_DOCS_DIR")) -> None:
    """Scan the docs folder once and update the index."""
    from rug.indexer import Indexer
    from rug.llm import EmbeddingError, OllamaEmbedder

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    embedder = OllamaEmbedder()
    try:
        embedder.check()
    except EmbeddingError as e:
        typer.echo(f"ERROR {e}", err=True)
        raise typer.Exit(1) from e
    with make_session() as db:
        run = Indexer(db, embedder, docs_dir or get_settings().docs_dir).run()
        typer.echo(json.dumps({"run": run.id, "files": run.total, **run.counts}))
        for err in run.errors:
            typer.echo(f"ERROR {err['path']}: {err['error']}", err=True)


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


@app.command("gen-synthetic")
def gen_synthetic(out: Path = typer.Argument(..., help="Output folder")) -> None:
    """Write the synthetic evaluation corpus."""
    from eval.synthetic_gen import build

    for p in build(out):
        typer.echo(p)


if __name__ == "__main__":
    app()
