"""Background worker: keeps the index in step with the NAS and writes summaries.

Each cycle scans the share (like `rug ingest`), then writes pending summaries (like
`rug summarize`, yielding to live questions), then sleeps `RUG_SCAN_INTERVAL_S`. Every
expected failure (Ollama down, share unmounted, another scan running) is logged and the loop
carries on, so an outage costs a skipped cycle, never a restart loop.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from rug import embedmeta
from rug.config import Settings
from rug.indexer import Indexer, IndexerBusy, MassDeletionRefused
from rug.llm import ChatError, ChatModel, Embedder, EmbeddingError
from rug.summaries import live_questions_active, summarize_pending

log = logging.getLogger(__name__)


def run_cycle(
    factory: Callable[[], Session],
    embedder: Embedder,
    chat: ChatModel,
    settings: Settings,
    *,
    check_embedder: Callable[[], None] | None = None,
    check_chat: Callable[[], None] | None = None,
    poll_s: float = 5.0,
) -> dict[str, Any]:
    """One scan plus one summary pass. Returns what happened, per step."""
    out: dict[str, Any] = {}
    docs = Path(settings.docs_dir)
    try:
        if check_embedder:
            check_embedder()  # skip the scan while Ollama is down instead of failing every file
        with factory() as db:
            run = Indexer(db, embedder, docs, settings).run()
            out["scan"] = dict(run.counts)
            if run.errors:
                log.warning("scan finished with %d file error(s)", len(run.errors))
    except IndexerBusy:
        out["scan"] = "busy"
        log.info("another scan is running; skipping this one")
    except MassDeletionRefused as e:
        out["scan"] = "refused"
        log.error("%s", e)
    except FileNotFoundError as e:
        out["scan"] = "share-missing"
        log.error("document folder unavailable: %s", e)
    except embedmeta.EmbeddingModelMismatch as e:
        out["scan"] = "model-mismatch"
        log.error("scan skipped: %s", e)
    except EmbeddingError as e:
        out["scan"] = "embeddings-unavailable"
        log.warning("embeddings unavailable, scan skipped: %s", e)
    except Exception:
        out["scan"] = "error"
        log.exception("scan failed")

    try:
        if check_chat:
            check_chat()
        with factory() as db:

            def questions_waiting() -> bool:
                with factory() as probe:  # its own session: never touches the writer's
                    return live_questions_active(probe)

            counts = summarize_pending(
                db, chat, embedder, settings, yield_to=questions_waiting, poll_s=poll_s
            )
            out["summaries"] = dict(counts)
    except embedmeta.EmbeddingModelMismatch as e:
        out["summaries"] = "model-mismatch"
        log.error("summaries skipped: %s", e)
    except (ChatError, EmbeddingError) as e:
        out["summaries"] = "model-unavailable"
        log.warning("summaries skipped, model unavailable: %s", e)
    except Exception:
        out["summaries"] = "error"
        log.exception("summary pass failed")
    return out


def serve_forever(
    stop: threading.Event,
    factory: Callable[[], Session],
    embedder: Embedder,
    chat: ChatModel,
    settings: Settings,
    **checks: Callable[[], None],
) -> None:
    log.info("worker started; scanning %s every %ss", settings.docs_dir, settings.scan_interval_s)
    while not stop.is_set():
        result = run_cycle(factory, embedder, chat, settings, **checks)  # type: ignore[arg-type]
        log.info("cycle done: %s", result)
        stop.wait(settings.scan_interval_s)
    log.info("worker stopped")
