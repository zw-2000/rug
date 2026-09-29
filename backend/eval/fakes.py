"""Deterministic stand-ins for the models, so retrieval and plumbing can be evaluated (and
run in CI) without Ollama. They say nothing about answer quality: the fake chat model
merely quotes the first excerpt, so answer/citation scores are only meaningful live."""

import hashlib
import math
import re


class HashEmbedder:
    """Bag-of-words hashing embedder. Counts calls so tests can assert nothing was re-embedded."""

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim
        self.calls = 0
        self.texts = 0

    def _vec(self, s: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"\w+", s.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big")
            v[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts += len(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class ExtractiveChat:
    """Quotes the start of excerpt [1] and cites it. Never says "not found"."""

    def chat(self, messages: list[dict[str, str]], on_token=None) -> str:  # type: ignore[no-untyped-def]
        body = messages[-1]["content"]
        if "Excerpts:" not in body or "[1] " not in body:
            return "ok"
        first = body.split("[1] ", 1)[1].split("\n", 1)[1].split("\n\n", 1)[0]
        out = f"{first[:160].strip()} [1]"
        if on_token:
            on_token(out)
        return out
