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
    # Единая система HelpDeskEddy: чтение задач и отправка ответов
    base_url: str = os.getenv("HELPDESK_EDDY_BASE_URL", "https://support.lar.tech/ru")
    api_key: str = os.getenv("HELPDESK_EDDY_API_KEY", "")
    # Email учётки оператора — нужен для Basic-аутентификации API v2 (email:api_key)
    email: str = os.getenv("HELPDESK_EDDY_EMAIL", "")


@dataclass
class DeepSeekConfig:
    # OpenAI-совместимый сервер DeepSeek (подключение по доменному имени и порту)
    base_url: str = os.getenv("DEEPSEEK_BASE_URL", "http://chatbot.lar.tech:8081/v1")
    api_key: str = os.getenv("DEEPSEEK_API_KEY", "")  # сервер может не требовать ключа
    # Имя модели. Пусто → берём первую из GET /v1/models.
    model: str = os.getenv("DEEPSEEK_MODEL", "")
    temperature: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.2"))
    timeout: int = int(os.getenv("DEEPSEEK_TIMEOUT", "120"))


@dataclass
class RagConfig:
    # Qdrant: http(s)://host:port | путь на диске | ":memory:"
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    collection: str = os.getenv("QDRANT_COLLECTION", "metering_kb")
    # Локальная эмбеддинг-модель (fastembed). Мультиязычная — нужна для русского.
    embed_model: str = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-large")
    kb_dir: str = os.getenv("KB_DIR", "./knowledge_base")
    chunk_size: int = int(os.getenv("RAG_CHUNK_SIZE", "1200"))
    chunk_overlap: int = int(os.getenv("RAG_CHUNK_OVERLAP", "200"))
    top_k: int = int(os.getenv("RAG_TOP_K", "6"))


@dataclass
class AppConfig:
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    metering: MeteringServerConfig = field(default_factory=MeteringServerConfig)
    helpdesk_eddy: HelpDeskEddyConfig = field(default_factory=HelpDeskEddyConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    # Максимум ПУ в одной массовой операции (защита от перегрузки)
    max_bulk_size: int = int(os.getenv("MAX_BULK_SIZE", "50"))
    reports_dir: str = os.getenv("REPORTS_DIR", "./reports")


config = AppConfig()
