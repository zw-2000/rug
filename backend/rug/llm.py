"""Ollama clients: embeddings (indexing, retrieval) and chat (answers, summaries)."""

import json
from collections.abc import Callable
from typing import Protocol

import httpx

from rug.config import Settings, get_settings


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class EmbeddingError(RuntimeError):
    pass


class OllamaEmbedder:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.s = settings or get_settings()
        self.client = client or httpx.Client(base_url=self.s.ollama_url, timeout=120)

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(inputs), self.s.embed_batch):
            batch = inputs[i : i + self.s.embed_batch]
            r = self.client.post("/api/embed", json={"model": self.s.embed_model, "input": batch})
            if r.status_code != 200:
                raise EmbeddingError(f"Ollama /api/embed {r.status_code}: {r.text[:200]}")
            vecs = r.json()["embeddings"]
            if len(vecs) != len(batch) or any(len(v) != self.s.embed_dim for v in vecs):
                raise EmbeddingError(
                    f"expected {len(batch)} vectors of dim {self.s.embed_dim} from "
                    f"{self.s.embed_model}; check RUG_EMBED_MODEL / RUG_EMBED_DIM"
                )
            out.extend(vecs)
        return out

    def check(self) -> None:
        """Fail fast before a scan if Ollama is unreachable or the model is wrong."""
        try:
            self._embed(["ping"])
        except httpx.HTTPError as e:
            raise EmbeddingError(f"cannot reach Ollama at {self.s.ollama_url}: {e}") from e

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.s.embed_doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self.s.embed_query_prefix + text])[0]


class ChatError(RuntimeError):
    pass


class ChatModel(Protocol):
    def chat(
        self, messages: list[dict[str, str]], on_token: Callable[[str], None] | None = None
    ) -> str: ...


class OllamaChat:
    """Streaming /api/chat client. temperature 0 for repeatable answers; num_ctx is set on
    every request so the server's (smaller) default window never silently truncates a prompt."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.s = settings or get_settings()
        self.client = client or httpx.Client(
            base_url=self.s.ollama_url,
            timeout=httpx.Timeout(self.s.chat_timeout_s, connect=10),
        )

    def check(self) -> None:
        """Fail fast when Ollama is unreachable or the chat model has not been pulled."""
        model = self.s.chat_model
        try:
            r = self.client.post("/api/show", json={"model": model, "name": model})
        except httpx.HTTPError as e:
            raise ChatError(f"cannot reach Ollama at {self.s.ollama_url}: {e}") from e
        if r.status_code == 404:
            raise ChatError(f"chat model {model!r} is not installed; run: ollama pull {model}")
        if r.status_code != 200:
            raise ChatError(f"Ollama /api/show {r.status_code}: {r.text[:200]}")

    def chat(
        self, messages: list[dict[str, str]], on_token: Callable[[str], None] | None = None
    ) -> str:
        payload = {
            "model": self.s.chat_model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": 0, "num_ctx": self.s.chat_num_ctx},
        }
        parts: list[str] = []
        done = False
        try:
            with self.client.stream("POST", "/api/chat", json=payload) as r:
                if r.status_code != 200:
                    raise ChatError(f"Ollama /api/chat {r.status_code}: {r.read()[:200]!r}")
                for line in r.iter_lines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if "error" in obj:
                        raise ChatError(f"Ollama error: {obj['error']}")
                    token = (obj.get("message") or {}).get("content", "")
                    if token:
                        parts.append(token)
                        if on_token:
                            on_token(token)
                    if obj.get("done"):
                        done = True
                        break
        except httpx.HTTPError as e:
            raise ChatError(f"chat request failed: {e}") from e
        except json.JSONDecodeError as e:
            raise ChatError(f"malformed response line from Ollama: {e}") from e
        if not done:  # a cut-off answer must not look like a complete one
            raise ChatError("chat stream ended before the model finished")
        return "".join(parts)
