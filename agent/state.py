"""
Состояние графа LangGraph. Используем TypedDict (требование LangGraph для State),
Pydantic-модели — только для отдельных полей.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel

Intent = Literal["readings", "rejoin", "both", "unknown"]
ReadingsKind = Literal["daily", "last", "collection_map"]


class MeterRef(BaseModel):
    meter_id: str
    serial: str | None = None
    protocol: Literal["spodes", "non_spodes", "unknown"] = "unknown"
    object_name: str | None = None
    online: bool | None = None


class ParsedRequest(BaseModel):
    intent: Intent = "unknown"
    date_from: date | None = None
    date_to: date | None = None
    readings_kind: ReadingsKind = "daily"
    meter_queries: list[str] = []          # сырьё из заявки (серийники/адреса)
    use_dead_reboot: bool = False          # пользователь явно попросил DEAD после Реджойна
    raw_user_note: str | None = None


class TaskState(TypedDict, total=False):
    # вход
    task_id: str
    raw_text: str
    raw_payload: dict

    # после intake
    parsed: ParsedRequest

    # после lookup
    meters_resolved: list[MeterRef]
    meters_not_found: list[str]

    # результаты операций
    readings_result: dict           # {meter_id: [rows]}
    rejoin_results: list[dict]
    dead_results: list[dict]
    final_statuses: list[dict]

    # артефакты для клиента
    artifacts: list[str]            # пути к xlsx/png

    # предложенный текст ответа (граф НЕ отправляет его в HelpDesk)
    reply_text: str

    # ошибки нодов (накапливаем, не падаем)
    errors: Annotated[list[str], "append"]
