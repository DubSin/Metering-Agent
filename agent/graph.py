"""
LangGraph state-machine для обработки заявок ТП.

Поток:
  intake → lookup → router → [readings_flow | rejoin_flow | both] → report → compose_reply → END

Граф НЕ пишет в HelpDesk: финальный текст складывается в state.reply_text, а
отправку клиенту делает оператор после ревью в Telegram. К HelpDesk идут только
GET-запросы (в node_intake, чтобы дотянуть текст заявки).

LLM используется только в двух местах: intake (разбор текста заявки) и
compose_reply (формирование ответа клиенту).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from config import config
from tools import (
    build_readings_report,
    build_status_report,
    capture_metering_screenshot,
    get_connection_status,
    get_task_details,
    parse_non_spodes_readings,
    request_readings,
    search_meters,
    send_dead_reboot,
    send_rejoin,
)

from .prompts import COMPOSE_REPLY_SYSTEM, INTAKE_SYSTEM
from .state import MeterRef, ParsedRequest, TaskState

log = logging.getLogger(__name__)


def _llm(temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=config.openai_model,
        temperature=temperature,
        api_key=config.openai_api_key,
    )


# ---------- ноды ----------

async def node_intake(state: TaskState) -> dict:
    """Разбираем текст заявки в ParsedRequest через LLM (structured output)."""
    text = state.get("raw_text") or ""
    if not text and state.get("task_id"):
        # текст не пришёл с webhook — дотягиваем из HelpDesk
        details = await get_task_details.ainvoke({"task_id": state["task_id"]})
        text = details.get("text") or details.get("subject") or ""
        state_patch: dict = {"raw_payload": {**(state.get("raw_payload") or {}), "fetched": details}}
    else:
        state_patch = {}

    llm = _llm().with_structured_output(ParsedRequest)
    try:
        parsed: ParsedRequest = await llm.ainvoke([
            SystemMessage(content=INTAKE_SYSTEM),
            HumanMessage(content=text),
        ])
    except Exception as e:
        log.exception("intake LLM failed")
        return {**state_patch, "parsed": ParsedRequest(), "errors": [f"intake: {e}"]}

    # дефолтная дата для readings — сегодня
    if parsed.intent in ("readings", "both") and parsed.date_from is None:
        parsed.date_from = parsed.date_to = date.today()
    if parsed.date_from and parsed.date_to is None:
        parsed.date_to = parsed.date_from

    log.info("intake parsed: intent=%s meters=%d", parsed.intent, len(parsed.meter_queries))
    return {**state_patch, "parsed": parsed}


async def node_lookup(state: TaskState) -> dict:
    """Ищем каждый запрос пользователя в Metering Server."""
    parsed: ParsedRequest = state["parsed"]
    resolved: list[MeterRef] = []
    not_found: list[str] = []

    if not parsed.meter_queries:
        return {"meters_resolved": [], "meters_not_found": [], "errors": ["lookup: пустой список ПУ"]}

    async def _search(q: str) -> tuple[str, list[dict]]:
        return q, await search_meters.ainvoke({"query": q})

    pairs = await asyncio.gather(*(_search(q) for q in parsed.meter_queries))
    for q, items in pairs:
        valid = [i for i in items if "error" not in i]
        if not valid:
            not_found.append(q)
            continue
        for it in valid:
            resolved.append(MeterRef(**{k: it.get(k) for k in MeterRef.model_fields if k in it}))

    log.info("lookup: resolved=%d not_found=%d", len(resolved), len(not_found))
    return {"meters_resolved": resolved, "meters_not_found": not_found}


def route_after_lookup(state: TaskState) -> str:
    parsed: ParsedRequest = state.get("parsed") or ParsedRequest()
    if not state.get("meters_resolved"):
        return "report"  # нечего делать — сразу собираем ответ с «не найдено»
    return {
        "readings": "readings_flow",
        "rejoin": "rejoin_flow",
        "both": "rejoin_flow",      # сначала на связь, потом показания
        "unknown": "report",
    }.get(parsed.intent, "report")


async def node_readings_flow(state: TaskState) -> dict:
    """Снимаем показания. Разделяем по протоколу."""
    parsed: ParsedRequest = state["parsed"]
    meters: list[MeterRef] = state["meters_resolved"]

    spodes_ids = [m.meter_id for m in meters if m.protocol == "spodes"]
    non_spodes_ids = [m.meter_id for m in meters if m.protocol == "non_spodes"]
    unknown_ids = [m.meter_id for m in meters if m.protocol == "unknown"]

    df = (parsed.date_from or date.today()).isoformat()
    dt = (parsed.date_to or parsed.date_from or date.today()).isoformat()

    combined: dict[str, list[dict]] = {}
    errors: list[str] = []

    if spodes_ids or unknown_ids:
        # unknown трактуем как СПОДЭС по умолчанию (Metering Server сам решит)
        ids = spodes_ids + unknown_ids
        res = await request_readings.ainvoke({
            "meter_ids": ids,
            "date_from": df,
            "date_to": dt,
            "kind": parsed.readings_kind,
        })
        if "error" in res:
            errors.append(f"readings(spodes): {res['error']}")
        combined.update(res.get("data", {}))

    if non_spodes_ids:
        res = await parse_non_spodes_readings.ainvoke({
            "meter_ids": non_spodes_ids,
            "date_from": df,
            "date_to": dt,
            "kind": parsed.readings_kind,
        })
        if res.get("not_implemented"):
            errors.append("readings(non_spodes): парсер не подключён")
        combined.update(res.get("data", {}))

    return {"readings_result": combined, "errors": errors}


async def node_rejoin_flow(state: TaskState) -> dict:
    """Реджойн → пауза → статус → опционально DEAD → финальный статус."""
    parsed: ParsedRequest = state["parsed"]
    meters: list[MeterRef] = state["meters_resolved"]
    ids = [m.meter_id for m in meters]

    rejoin = await send_rejoin.ainvoke({"meter_ids": ids})
    await asyncio.sleep(min(config.metering.command_timeout, 30))
    status1 = await get_connection_status.ainvoke({"meter_ids": ids})

    online_now = {s["meter_id"] for s in status1 if s.get("online")}
    offline = [m for m in ids if m not in online_now]

    dead: list[dict] = []
    if offline and parsed.use_dead_reboot:
        dead = await send_dead_reboot.ainvoke({"meter_ids": offline})
        await asyncio.sleep(min(config.metering.command_timeout, 30))
        status2 = await get_connection_status.ainvoke({"meter_ids": offline})
        # объединяем: для ПУ, что были offline, берём свежий статус
        merged = {s["meter_id"]: s for s in status1}
        for s in status2:
            merged[s["meter_id"]] = s
        final = list(merged.values())
    else:
        final = status1

    return {
        "rejoin_results": rejoin,
        "dead_results": dead,
        "final_statuses": final,
    }


def route_after_rejoin(state: TaskState) -> str:
    parsed: ParsedRequest = state["parsed"]
    if parsed.intent == "both":
        return "readings_flow"
    return "report"


async def node_report(state: TaskState) -> dict:
    """Сборка xlsx-отчётов + опц. скриншот."""
    task_id = state["task_id"]
    parsed: ParsedRequest = state.get("parsed") or ParsedRequest()
    artifacts: list[str] = []

    if state.get("readings_result"):
        path = await build_readings_report.ainvoke({
            "task_id": task_id,
            "readings": state["readings_result"],
            "kind": parsed.readings_kind,
        })
        artifacts.append(path)

    if state.get("final_statuses"):
        path = await build_status_report.ainvoke({
            "task_id": task_id,
            "statuses": state["final_statuses"],
        })
        artifacts.append(path)

    # скриншот «Журналов» — для readings/both. URL формируем условно;
    # TODO(metering_ui): точный URL страницы «Журналы» — заполнить при настройке.
    if parsed.intent in ("readings", "both") and state.get("meters_resolved"):
        first_mid = state["meters_resolved"][0].meter_id
        ui_url = f"{config.metering.base_url}/ui/meters/{first_mid}/journals"
        try:
            png = await capture_metering_screenshot.ainvoke({
                "task_id": task_id,
                "page_url": ui_url,
            })
            if png and not png.startswith("Playwright"):
                artifacts.append(png)
        except Exception as e:
            log.warning("screenshot skipped: %s", e)

    return {"artifacts": artifacts}


async def node_compose_reply(state: TaskState) -> dict:
    """LLM собирает текст ответа из state."""
    parsed: ParsedRequest = state.get("parsed") or ParsedRequest()
    summary = {
        "intent": parsed.intent,
        "date_from": parsed.date_from.isoformat() if parsed.date_from else None,
        "date_to": parsed.date_to.isoformat() if parsed.date_to else None,
        "readings_kind": parsed.readings_kind,
        "resolved": [m.model_dump() for m in state.get("meters_resolved", [])],
        "not_found": state.get("meters_not_found", []),
        "statuses": state.get("final_statuses", []),
        "has_readings": bool(state.get("readings_result")),
        "artifacts_count": len(state.get("artifacts", [])),
        "errors": state.get("errors", []),
    }
    msg = await _llm(temperature=0.2).ainvoke([
        SystemMessage(content=COMPOSE_REPLY_SYSTEM),
        HumanMessage(content=json.dumps(summary, ensure_ascii=False, default=str)),
    ])
    return {"reply_text": msg.content}


# ---------- сборка графа ----------

def build_graph():
    g = StateGraph(TaskState)

    g.add_node("intake", node_intake)
    g.add_node("lookup", node_lookup)
    g.add_node("readings_flow", node_readings_flow)
    g.add_node("rejoin_flow", node_rejoin_flow)
    g.add_node("report", node_report)
    g.add_node("compose_reply", node_compose_reply)

    g.set_entry_point("intake")
    g.add_edge("intake", "lookup")

    g.add_conditional_edges("lookup", route_after_lookup, {
        "readings_flow": "readings_flow",
        "rejoin_flow": "rejoin_flow",
        "report": "report",
    })

    g.add_edge("readings_flow", "report")

    g.add_conditional_edges("rejoin_flow", route_after_rejoin, {
        "readings_flow": "readings_flow",
        "report": "report",
    })

    g.add_edge("report", "compose_reply")
    g.add_edge("compose_reply", END)

    return g.compile()


_graph = None


def graph_singleton():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


async def run_task(task_id: str, raw_text: str = "", raw_payload: dict | None = None) -> dict:
    """Точка входа: запустить граф для одной заявки HelpDesk."""
    initial: TaskState = {
        "task_id": task_id,
        "raw_text": raw_text,
        "raw_payload": raw_payload or {},
        "errors": [],
    }
    out = await graph_singleton().ainvoke(initial)
    log.info("task done: id=%s errors=%s", task_id, out.get("errors"))
    return out
