"""Тесты клиента DeepSeek. Сеть не нужна — httpx.MockTransport подменяет сервер."""
import json

import httpx
import pytest

from rag.llm import DeepSeekClient


@pytest.fixture
def mock_httpx(monkeypatch):
    """Подменяет httpx.Client внутри rag.llm заглушкой с MockTransport.

    Возвращает список перехваченных запросов (для проверки payload).
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]},
            )
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "model": "deepseek-chat",
                    "choices": [{"message": {"role": "assistant", "content": "инструкция"}}],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("rag.llm.httpx.Client", client_factory)
    return captured


def test_list_models(mock_httpx):
    client = DeepSeekClient(base_url="http://x/v1", model="deepseek-chat")
    assert client.list_models() == ["deepseek-chat", "deepseek-reasoner"]


def test_model_autoselect_first(mock_httpx):
    # model не задан → берётся первый из /v1/models
    client = DeepSeekClient(base_url="http://x/v1", model="")
    assert client.model == "deepseek-chat"


def test_chat_payload_and_parse(mock_httpx):
    client = DeepSeekClient(base_url="http://x/v1", model="deepseek-chat")
    out = client.chat([{"role": "user", "content": "привет"}], temperature=0.1)

    assert out["text"] == "инструкция"
    assert out["model"] == "deepseek-chat"

    # проверяем, что отправили на /chat/completions с нужным payload
    post = next(r for r in mock_httpx if r.url.path.endswith("/chat/completions"))
    body = json.loads(post.content)
    assert body["model"] == "deepseek-chat"
    assert body["stream"] is False
    assert body["temperature"] == 0.1
    assert body["messages"][0]["content"] == "привет"


def test_auth_header_when_key_set(mock_httpx):
    client = DeepSeekClient(base_url="http://x/v1", api_key="secret", model="deepseek-chat")
    client.list_models()
    req = mock_httpx[-1]
    assert req.headers.get("Authorization") == "Bearer secret"
