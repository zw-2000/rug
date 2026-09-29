import json

import httpx
import pytest

from rug.config import Settings
from rug.llm import EmbeddingError, OllamaEmbedder


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))


def test_batches_and_prefixes():
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json={"embeddings": [[0.1] * 4 for _ in body["input"]]})

    s = Settings(embed_dim=4, embed_batch=2, embed_model="m")
    e = OllamaEmbedder(s, _client(handler))
    vecs = e.embed_documents(["a", "b", "c"])
    assert len(vecs) == 3
    assert [len(b["input"]) for b in seen] == [2, 1]
    assert seen[0] == {"model": "m", "input": ["search_document: a", "search_document: b"]}
    e.embed_query("q")
    assert seen[-1]["input"] == ["search_query: q"]


def test_wrong_dimension_is_loud():
    s = Settings(embed_dim=768)
    e = OllamaEmbedder(s, _client(lambda r: httpx.Response(200, json={"embeddings": [[0.0] * 3]})))
    with pytest.raises(EmbeddingError, match="dim 768"):
        e.embed_documents(["x"])


def test_http_error_is_loud():
    e = OllamaEmbedder(Settings(), _client(lambda r: httpx.Response(404, text="model not found")))
    with pytest.raises(EmbeddingError, match="404"):
        e.embed_documents(["x"])


def test_check_reports_unreachable_ollama():
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(EmbeddingError, match="cannot reach Ollama"):
        OllamaEmbedder(Settings(), _client(refuse)).check()
