"""
Минимальный клиент нативного API ER-GPT (v2 one-shot).

Stateless-вызов: один запрос — один ответ, без сохранения чата.
Системный промпт передаётся через assistant.prompt.

Эндпоинт: POST {base_url}/api/v2/message/one-shot
Авторизация: Bearer-токен.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from config import config

log = logging.getLogger(__name__)


class ERGPTError(RuntimeError):
    pass


async def one_shot(
    *,
    content: str,
    system_prompt: str,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> str:
    """Одиночный запрос к ER-GPT. Возвращает текст ответа модели."""
    if not config.ergpt_api_key:
        raise ERGPTError("ERGPT_API_KEY не задан в окружении")

    payload: dict[str, Any] = {
        "content": content,
        "model_id": config.ergpt_model,
        "assistant": {"prompt": system_prompt, "temperature": temperature},
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    url = f"{config.ergpt_base_url.rstrip('/')}/api/v2/message/one-shot"
    headers = {
        "Authorization": f"Bearer {config.ergpt_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=config.ergpt_timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise ERGPTError(f"ER-GPT HTTP {resp.status_code}: {resp.text}")
        data = resp.json()

    try:
        return data["message"]["content"]
    except (KeyError, TypeError) as e:
        raise ERGPTError(f"Неожиданный формат ответа ER-GPT: {data!r}") from e


async def one_shot_json(
    *,
    content: str,
    system_prompt: str,
    schema: dict[str, Any] | None = None,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """
    То же, что one_shot, но ждём от модели JSON и парсим его.

    Если передана JSON-схема, она дописывается в системный промпт —
    это страховка на случай, если основной промпт её не описывает.
    Поддерживает ответы, обёрнутые в ```json ... ``` (модель иногда так делает).
    """
    sys = system_prompt
    if schema is not None:
        sys = (
            f"{system_prompt}\n\n"
            "Возвращай ТОЛЬКО валидный JSON, соответствующий следующей JSON-схеме, "
            "без пояснений и без блоков кода:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )

    raw = await one_shot(content=content, system_prompt=sys, temperature=temperature)
    text = raw.strip()

    # снимаем возможную обёртку ```json ... ```
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ERGPTError(f"Модель вернула не-JSON: {raw!r}") from e
