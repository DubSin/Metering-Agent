"""
Клиент OpenAI-совместимого LLM-сервера (DeepSeek или Ollama).

Эндпоинты:
  GET  {base_url}/models            — список моделей
  POST {base_url}/chat/completions  — запрос к модели

Провайдер выбирается через config.llm_provider ("deepseek" | "ollama").
Фабрика make_llm() собирает клиент с нужными base_url/model/ключом.

DeepSeek-V4: всегда передаём reasoning_effort="minimal", иначе модель
тратит сотни токенов на внутренние рассуждения до выдачи ответа.
"""
from __future__ import annotations

import logging

import httpx

from config import config

log = logging.getLogger(__name__)


def _raise_for_status(r: httpx.Response) -> None:
    """raise_for_status, но с телом ответа в тексте ошибки.

    Ollama/DeepSeek кладут причину 4xx/5xx в тело ({"error": "..."}),
    а стандартный raise_for_status его теряет.
    """
    if r.is_success:
        return
    body = r.text.strip()
    raise httpx.HTTPStatusError(
        f"{r.status_code} от {r.request.url}: {body[:1000] or '(пустое тело)'}",
        request=r.request,
        response=r,
    )


class DeepSeekClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.base_url = (base_url or config.deepseek.base_url).rstrip("/")
        self.api_key = api_key or config.deepseek.api_key
        self._model = model or config.deepseek.model
        self.timeout = timeout or config.deepseek.timeout
        self._default_temperature = config.deepseek.temperature
        # "minimal" по умолчанию — быстрый ответ без длинных рассуждений
        self.reasoning_effort = reasoning_effort or config.deepseek.reasoning_effort

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def list_models(self) -> list[str]:
        """GET /v1/models → список id моделей."""
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.base_url}/models", headers=self._headers())
            _raise_for_status(r)
            data = r.json().get("data") or []
        return [m.get("id") for m in data if m.get("id")]

    @property
    def model(self) -> str:
        """Имя модели. Если не задано — берём первую из /v1/models."""
        if not self._model:
            ids = self.list_models()
            if not ids:
                raise RuntimeError(
                    f"Сервер {self.base_url} не вернул ни одной модели (/models)"
                )
            self._model = ids[0]
            log.info("DeepSeek: модель выбрана автоматически — %s", self._model)
        return self._model

    def chat(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """POST /v1/chat/completions. Возвращает {text, model, reasoning, raw}."""
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": (
                self._default_temperature if temperature is None else temperature
            ),
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

        with httpx.Client(timeout=self.timeout) as c:
            r = c.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            _raise_for_status(r)
            data = r.json()

        message = data["choices"][0]["message"]
        reasoning = message.get("reasoning_content") or ""
        if reasoning:
            log.debug("DeepSeek reasoning (%d chars): %s…", len(reasoning), reasoning[:120])
        return {
            "text": message.get("content") or "",
            "model": data.get("model", self.model),
            "reasoning": reasoning,
            "raw": data,
        }


def make_llm(provider: str | None = None) -> DeepSeekClient:
    """Собрать LLM-клиент для генерации в RAG по выбранному провайдеру.

    provider: "deepseek" (по умолчанию) или "ollama". Если не передан —
    берётся config.llm_provider. Оба — OpenAI-совместимые, поэтому
    используется один и тот же класс с разными base_url/model/ключом.
    """
    provider = (provider or config.llm_provider or "deepseek").strip().lower()
    if provider == "ollama":
        client = DeepSeekClient(
            base_url=config.ollama.base_url,
            api_key=config.ollama.api_key,
            model=config.ollama.model,
            timeout=config.ollama.timeout,
        )
        client._default_temperature = config.ollama.temperature
        # Ollama не понимает reasoning_effort — не отправляем это поле.
        client.reasoning_effort = ""
        return client
    if provider == "deepseek":
        return DeepSeekClient(
            base_url=config.deepseek.base_url,
            api_key=config.deepseek.api_key,
            model=config.deepseek.model,
            timeout=config.deepseek.timeout,
            reasoning_effort=config.deepseek.reasoning_effort,
        )
    raise ValueError(
        f"Неизвестный LLM_PROVIDER: {provider!r}. Допустимо: deepseek | ollama."
    )
