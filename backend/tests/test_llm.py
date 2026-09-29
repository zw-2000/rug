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


def _chat(lines: list[str], status: int = 200, seen: list | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(status, content=("\n".join(lines) + "\n").encode())

    from rug.llm import OllamaChat

    return OllamaChat(Settings(chat_model="m", chat_num_ctx=4096), _client(handler))


def test_chat_streams_tokens_and_pins_the_context_window():
    seen: list = []
    lines = [
        '{"message":{"role":"assistant","content":"Hel"},"done":false}',
        '{"message":{"role":"assistant","content":"lo"},"done":false}',
        '{"message":{"role":"assistant","content":""},"done":true}',
    ]
    tokens: list[str] = []
    out = _chat(lines, seen=seen).chat([{"role": "user", "content": "hi"}], tokens.append)
    assert out == "Hello" and tokens == ["Hel", "lo"]
    path, body = seen[0]
    assert path == "/api/chat" and body["stream"] is True and body["model"] == "m"
    assert body["options"] == {"temperature": 0, "num_ctx": 4096}


def test_chat_refuses_a_truncated_stream():
    from rug.llm import ChatError

    lines = ['{"message":{"role":"assistant","content":"Half an ans"},"done":false}']
    with pytest.raises(ChatError, match="ended before"):
        _chat(lines).chat([{"role": "user", "content": "hi"}])


def test_chat_surfaces_server_errors():
    from rug.llm import ChatError

    with pytest.raises(ChatError, match="model requires more system memory"):
        _chat(['{"error":"model requires more system memory"}']).chat([])
    with pytest.raises(ChatError, match="500"):
        _chat(["boom"], status=500).chat([])


def test_chat_check_reports_missing_model_and_unreachable_server():
    from rug.llm import ChatError, OllamaChat

    missing = OllamaChat(Settings(chat_model="nope"), _client(lambda r: httpx.Response(404)))
    with pytest.raises(ChatError, match="ollama pull nope"):
        missing.check()

    def refuse(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ChatError, match="cannot reach"):
        OllamaChat(Settings(), _client(refuse)).check()
    OllamaChat(Settings(), _client(lambda r: httpx.Response(200, json={}))).check()
