"""
Конфигурация API-эндпоинтов.
Заполни значения перед запуском.
"""

import os
from dataclasses import dataclass, field


@dataclass
class MeteringServerConfig:
    base_url: str = os.getenv("METERING_BASE_URL", "http://METERING_SERVER_HOST:PORT")
    username: str = os.getenv("METERING_USER", "")
    password: str = os.getenv("METERING_PASS", "")
    # Порты команд (из постановки задачи)
    rejoin_port: int = 223
    rejoin_cmd: str = "0FFF"
    dead_port: int = 201
    dead_cmd: str = "DEAD"
    # Таймаут ожидания ответа от ПУ (секунды)
    command_timeout: int = 30


@dataclass
class HelpDeskEddyConfig:
    """HelpDeskEddy API v2.

    Аутентификация: Basic base64(email:api_key). API-ключ выдаётся главным
    администратором и виден в профиле пользователя.

    base_url — только хост, без /api/v2 (префикс добавляет клиент).
    """
    base_url: str = os.getenv("HELPDESK_EDDY_BASE_URL")
    email: str = os.getenv("HELPDESK_EDDY_EMAIL", "")
    api_key: str = os.getenv("HELPDESK_EDDY_API_KEY", "")
    # Опц. секрет HMAC для проверки исходящих webhook'ов (если настроен в HDE).
    webhook_secret: str = os.getenv("HELPDESK_EDDY_WEBHOOK_SECRET", "")
    # Канал ответа: "comments" (видны только сотрудникам) или "posts" (видны клиенту).
    # По решению по проекту: всегда внутренний комментарий.
    reply_channel: str = os.getenv("HELPDESK_EDDY_REPLY_CHANNEL", "comments")
    # Статус, в который переводим тикет после успешного ответа. Пусто = не менять.
    post_reply_status: str = os.getenv("HELPDESK_EDDY_POST_REPLY_STATUS", "process")
    # Фильтр поллера. По решению: только новые (open).
    poll_status_list: str = os.getenv("HELPDESK_EDDY_POLL_STATUSES", "open")
    # Лимит RPM на стороне HDE — 300/мин, блокировка 20 мин при превышении.
    request_timeout: float = float(os.getenv("HELPDESK_EDDY_TIMEOUT", "30"))


@dataclass
class AppConfig:
    ergpt_api_key: str = field(
        default_factory=lambda: os.getenv("ERGPT_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )
    ergpt_base_url: str = os.getenv("ERGPT_BASE_URL")
    ergpt_model: str = os.getenv("ERGPT_MODEL") or os.getenv(
        "OPENAI_MODEL", "ERGPT-Main"
    )
    ergpt_timeout: float = float(os.getenv("ERGPT_TIMEOUT", "120"))
    # ID базы знаний ER-GPT. Пока пусто — RAG-нода работает как заглушка
    # (вызов LLM без kb_id). Когда KB будет настроена (Chroma/Qdrant + загрузка
    # в портал ER-GPT) — заполните переменную и нода автоматически начнёт
    # использовать RAG.
    ergpt_kb_id: str = os.getenv("ERGPT_KB_ID", "")

    metering: MeteringServerConfig = field(default_factory=MeteringServerConfig)
    helpdesk_eddy: HelpDeskEddyConfig = field(default_factory=HelpDeskEddyConfig)

    # Полер HelpDeskEddy (страховка на случай пропущенного webhook).
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    processed_tickets_path: str = os.getenv(
        "PROCESSED_TICKETS_PATH", "./state/processed_tickets.txt"
    )
    poll_cursor_path: str = os.getenv("POLL_CURSOR_PATH", "./state/poll_cursor.txt")

    # Максимум ПУ в одной массовой операции (защита от перегрузки)
    max_bulk_size: int = int(os.getenv("MAX_BULK_SIZE", "50"))
    reports_dir: str = os.getenv("REPORTS_DIR", "./reports")


config = AppConfig()
