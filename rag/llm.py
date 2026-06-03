"""
Клиент OpenAI-совместимого сервера DeepSeek.

Подключение по доменному имени и порту, эндпоинты:
  GET  {base_url}/models            — список моделей
  POST {base_url}/chat/completions  — запрос к модели

base_url задаётся в config.deepseek (по умолчанию http://chatbot.lar.tech:8081/v1).
"""
from __future__ import annotations

import logging

import httpx

from config import config

log = logging.getLogger(__name__)


class DeepSeekClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or config.deepseek.base_url).rstrip("/")
        self.api_key = api_key or config.deepseek.api_key
        self._model = model or config.deepseek.model
        self.timeout = timeout or config.deepseek.timeout

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def list_models(self) -> list[str]:
        """GET /v1/models → список id моделей."""
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.base_url}/models", headers=self._headers())
            r.raise_for_status()
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
        """POST /v1/chat/completions. Возвращает {text, model, raw}."""
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": (
                config.deepseek.temperature if temperature is None else temperature
            ),
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        with httpx.Client(timeout=self.timeout) as c:
            r = c.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data = r.json()

        message = data["choices"][0]["message"]
        return {
            "text": message.get("content") or "",
            "model": data.get("model", self.model),
            "raw": data,
        }
