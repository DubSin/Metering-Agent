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
class EddyConfig:
    base_url: str = os.getenv("EDDY_BASE_URL", "http://EDDY_HOST:PORT")
    username: str = os.getenv("EDDY_USER", "")
    password: str = os.getenv("EDDY_PASS", "")


@dataclass
class HelpDeskConfig:
    # Адрес HelpDesk для чтения задач и отправки ответов
    base_url: str = os.getenv("HELPDESK_BASE_URL", "http://HELPDESK_HOST:PORT")
    api_key: str = os.getenv("HELPDESK_API_KEY", "")


@dataclass
class AppConfig:
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    metering: MeteringServerConfig = field(default_factory=MeteringServerConfig)
    eddy: EddyConfig = field(default_factory=EddyConfig)
    helpdesk: HelpDeskConfig = field(default_factory=HelpDeskConfig)
    # Максимум ПУ в одной массовой операции (защита от перегрузки)
    max_bulk_size: int = int(os.getenv("MAX_BULK_SIZE", "50"))
    reports_dir: str = os.getenv("REPORTS_DIR", "./reports")


config = AppConfig()
