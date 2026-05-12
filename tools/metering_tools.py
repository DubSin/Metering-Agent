"""
Инструменты работы с Metering Server (REST API) и парсером не-СПОДЭС.

Все эндпоинты помечены TODO(endpoint) — заполнить при интеграции.
Адреса/креды берутся из config.py через env vars.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Literal

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel

from config import config

log = logging.getLogger(__name__)


# ---------- модели ----------

Protocol = Literal["spodes", "non_spodes", "unknown"]
ReadingsKind = Literal["daily", "last", "collection_map"]


class MeterInfo(BaseModel):
    meter_id: str
    serial: str | None = None
    protocol: Protocol = "unknown"
    object_name: str | None = None
    address: str | None = None
    online: bool | None = None


class ReadingRow(BaseModel):
    meter_id: str
    timestamp: str
    register: str
    value: float
    unit: str = "kWh"


class OperationResult(BaseModel):
    meter_id: str
    ok: bool
    detail: str = ""
    raw: dict | None = None


# ---------- HTTP-клиент ----------

def _client() -> httpx.AsyncClient:
    auth = None
    if config.metering.username:
        auth = (config.metering.username, config.metering.password)
    return httpx.AsyncClient(
        base_url=config.metering.base_url,
        auth=auth,
        timeout=config.metering.command_timeout,
    )


async def _post_command(meter_id: str, port: int, payload: str) -> OperationResult:
    """Отправка команды на ПУ через Metering Server."""
    # TODO(endpoint): уточнить путь команды в API Metering Server,
    #   ниже — псевдо-эндпоинт, заменить при настройке.
    url = "/api/v1/meters/{meter_id}/command".format(meter_id=meter_id)
    body = {"port": port, "payload": payload}
    try:
        async with _client() as c:
            r = await c.post(url, json=body)
            r.raise_for_status()
            data = r.json()
        ok = bool(data.get("success", True))
        return OperationResult(
            meter_id=meter_id,
            ok=ok,
            detail=data.get("message", ""),
            raw=data,
        )
    except httpx.HTTPError as e:
        log.exception("command failed: meter=%s port=%s payload=%s", meter_id, port, payload)
        return OperationResult(meter_id=meter_id, ok=False, detail=f"HTTP error: {e}")


# ---------- tools для LangChain ----------

@tool
async def search_meters(query: str) -> list[dict]:
    """Найти ПУ в Metering Server по серийному номеру/адресу/имени объекта.

    Возвращает список найденных ПУ с протоколом (СПОДЭС/не-СПОДЭС) и статусом online.
    Использовать перед любыми операциями над ПУ.
    """
    # TODO(endpoint): путь поиска в API Metering Server
    url = "/api/v1/meters/search"
    try:
        async with _client() as c:
            r = await c.get(url, params={"q": query})
            r.raise_for_status()
            items = r.json().get("items", [])
    except httpx.HTTPError as e:
        log.exception("search_meters failed: %s", query)
        return [{"error": str(e), "query": query}]
    return [MeterInfo(**i).model_dump() for i in items]


@tool
async def request_readings(
    meter_ids: list[str],
    date_from: str,
    date_to: str,
    kind: ReadingsKind = "daily",
) -> dict:
    """Запрос показаний по списку ПУ за диапазон дат.

    Args:
        meter_ids: список идентификаторов ПУ из Metering Server.
        date_from: ISO-дата начала диапазона, YYYY-MM-DD.
        date_to: ISO-дата конца диапазона, YYYY-MM-DD.
        kind: тип отчёта — daily | last | collection_map.

    Для СПОДЭС-ПУ команды уходят в Metering Server (вкладка «Журналы»).
    Для не-СПОДЭС — используется парсер (см. parse_non_spodes_readings ниже).
    Маршрутизацию решает caller (граф) после lookup'а.
    Возвращает: {meter_id: [ReadingRow, ...]} плюс errors.
    """
    if len(meter_ids) > config.max_bulk_size:
        return {
            "error": f"bulk size {len(meter_ids)} > max_bulk_size {config.max_bulk_size}",
            "data": {},
        }

    # TODO(endpoint): «Журналы» — путь чтения показаний по СПОДЭС
    url = "/api/v1/meters/readings"
    body = {
        "meter_ids": meter_ids,
        "date_from": date_from,
        "date_to": date_to,
        "kind": kind,
    }
    try:
        async with _client() as c:
            r = await c.post(url, json=body)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        log.exception("request_readings failed: %s", meter_ids)
        return {"error": str(e), "data": {}}
    return data


@tool
async def parse_non_spodes_readings(
    meter_ids: list[str],
    date_from: str,
    date_to: str,
    kind: ReadingsKind = "daily",
) -> dict:
    """Получить показания не-СПОДЭС ПУ через спецпарсер.

    TODO(parser): подключить вызов реального парсера (CLI или HTTP).
    Сейчас — заглушка, возвращает структуру с пометкой not_implemented.
    """
    return {
        "data": {},
        "not_implemented": True,
        "meter_ids": meter_ids,
        "date_from": date_from,
        "date_to": date_to,
        "kind": kind,
    }


@tool
async def send_rejoin(meter_ids: list[str]) -> list[dict]:
    """Реджойн: команда 0FFF на порт 223 — попытка вывести ПУ на связь.

    Подходит для любых типов ПУ. Возвращает список результатов по каждому ПУ.
    """
    if len(meter_ids) > config.max_bulk_size:
        return [{"error": f"bulk size {len(meter_ids)} > max_bulk_size {config.max_bulk_size}"}]

    sem = asyncio.Semaphore(8)

    async def _one(mid: str) -> OperationResult:
        async with sem:
            return await _post_command(mid, config.metering.rejoin_port, config.metering.rejoin_cmd)

    results = await asyncio.gather(*(_one(m) for m in meter_ids))
    return [r.model_dump() for r in results]


@tool
async def send_dead_reboot(meter_ids: list[str]) -> list[dict]:
    """Перезагрузка счётчика: команда DEAD на порт 201.

    Использовать, если Реджойн не помог. Список ПУ — короткий (точечно).
    """
    if len(meter_ids) > config.max_bulk_size:
        return [{"error": f"bulk size {len(meter_ids)} > max_bulk_size {config.max_bulk_size}"}]

    sem = asyncio.Semaphore(4)

    async def _one(mid: str) -> OperationResult:
        async with sem:
            return await _post_command(mid, config.metering.dead_port, config.metering.dead_cmd)

    results = await asyncio.gather(*(_one(m) for m in meter_ids))
    return [r.model_dump() for r in results]


@tool
async def get_connection_status(meter_ids: list[str]) -> list[dict]:
    """Проверить, на связи ли ПУ. Запрашивается после Реджойна/DEAD.

    Возвращает [{meter_id, online: bool, last_seen: iso-str}, ...].
    """
    # TODO(endpoint): путь статуса в API Metering Server
    url = "/api/v1/meters/status"
    try:
        async with _client() as c:
            r = await c.get(url, params={"meter_ids": ",".join(meter_ids)})
            r.raise_for_status()
            return r.json().get("items", [])
    except httpx.HTTPError as e:
        log.exception("get_connection_status failed: %s", meter_ids)
        return [{"meter_id": m, "online": None, "error": str(e)} for m in meter_ids]
