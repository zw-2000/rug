"""Ollama client. M1 only needs embeddings; chat arrives with the RAG milestone."""

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
