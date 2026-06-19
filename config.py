"""
Конфигурация API-эндпоинтов.
Заполни значения перед запуском.
"""

import os
from dataclasses import dataclass, field

# .env грузим ЗДЕСЬ — до чтения переменных в телах @dataclass ниже. config
# импортируется раньше всего (в т.ч. транзитивно из rag/__init__.py), поэтому
# load_dotenv() в отдельных entrypoint'ах (run.py, rag.index) для standalone
# запусков `python -m rag.* / poller` срабатывает слишком поздно — значения
# уже «заморожены» на дефолтах. Загрузка здесь чинит это для всех входов.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


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
    # Единая система HelpDeskEddy: чтение задач (только GET в пайплайне)
    base_url: str = os.getenv("HELPDESK_EDDY_BASE_URL", "https://support.lar.tech/ru")
    api_key: str = os.getenv("HELPDESK_EDDY_API_KEY", "")
    # Email учётки оператора — нужен для Basic-аутентификации API v2 (email:api_key)
    email: str = os.getenv("HELPDESK_EDDY_EMAIL", "")
    # Шаблон ссылки на тикет для сообщения оператору. {id} подставляется.
    ticket_url_template: str = os.getenv(
        "HELPDESK_TICKET_URL_TEMPLATE",
        "https://support.lar.tech/ru/ticket/list/filter/id/1/ticket/{id}",
    )


@dataclass
class TelegramConfig:
    # Бот для human-in-the-loop ревью тикетов
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # Групповой чат, куда идёт рассылка тикетов на ревью
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    api_base: str = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
    # Прокси ТОЛЬКО для запросов к Telegram (http://… или socks5://…).
    # Нужен, когда api.telegram.org заблокирован у провайдера/хостинга.
    # Пусто = без прокси. Не влияет на HelpDesk/DeepSeek/Ollama.
    proxy: str = os.getenv("TELEGRAM_PROXY", "")
    # Таймаут long-polling getUpdates (секунды)
    poll_timeout: int = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
    # Белый список пользователей, которым разрешено управлять ботом
    # (кнопки и команды). Через запятую/пробел: numeric user_id и/или @username.
    # ПУСТО = разрешено всем (открытый режим).
    allowed_users: tuple[str, ...] = tuple(
        e.strip().lstrip("@").lower()
        for e in os.getenv("TELEGRAM_ALLOWED_USERS", "").replace(",", " ").split()
        if e.strip()
    )


@dataclass
class DeepSeekConfig:
    # OpenAI-совместимый сервер DeepSeek (подключение по доменному имени и порту)
    base_url: str = os.getenv("DEEPSEEK_BASE_URL", "http://chatbot.lar.tech:8080/v1")
    api_key: str = os.getenv("DEEPSEEK_API_KEY", "")  # сервер может не требовать ключа
    # Имя модели. Пусто → берём первую из GET /v1/models.
    model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    # 0.2 — почти детерминированно (дословные команды/пароли из базы), но чуть живее
    # формулировки. Предполагаемые инструкции обеспечивает промпт, а не температура.
    temperature: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.2"))
    timeout: int = int(os.getenv("DEEPSEEK_TIMEOUT", "120"))
    # "minimal" — быстрый ответ без цепочки рассуждений. Пусто — не передаём.
    reasoning_effort: str = os.getenv("DEEPSEEK_REASONING_EFFORT", "minimal")


@dataclass
class OllamaConfig:
    # OpenAI-совместимый эндпоинт Ollama (POST /v1/chat/completions).
    base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    api_key: str = os.getenv("OLLAMA_API_KEY", "ollama")  # Ollama ключ не проверяет
    # Имя модели. Пусто → берём первую из GET /v1/models.
    model: str = os.getenv("OLLAMA_MODEL", "")
    # 0 — детерминированные ответы строго по базе знаний, без фантазий.
    temperature: float = float(os.getenv("OLLAMA_TEMPERATURE", "0"))
    timeout: int = int(os.getenv("OLLAMA_TIMEOUT", "120"))


@dataclass
class RagConfig:
    # Qdrant: http(s)://host:port | путь на диске | ":memory:"
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    collection: str = os.getenv("QDRANT_COLLECTION", "metering_kb")
    # Локальная эмбеддинг-модель (fastembed). Мультиязычная — нужна для русского.
    embed_model: str = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-large")
    kb_dir: str = os.getenv("KB_DIR", "./knowledge_base")
    # xlsx-справочник клиентов/owner (расшифровка кодов и сокращений из тикета).
    # Файла нет → пайплайн работает без справочника.
    directory_path: str = os.getenv("DIRECTORY_XLSX", "./data/clients.xlsx")
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
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    # Провайдер LLM для генерации в RAG: "deepseek" | "ollama"
    llm_provider: str = os.getenv("LLM_PROVIDER", "deepseek")
    rag: RagConfig = field(default_factory=RagConfig)
    # Максимум ПУ в одной массовой операции (защита от перегрузки)
    max_bulk_size: int = int(os.getenv("MAX_BULK_SIZE", "50"))
    reports_dir: str = os.getenv("REPORTS_DIR", "./reports")
    # SQLite с решениями операторов (датасет для дообучения)
    feedback_db: str = os.getenv("FEEDBACK_DB", "./data/feedback.sqlite3")
    # Ручная подтяжка тикетов из бота (команда /fetch) и автоопрос поллером
    fetch_statuses: str = os.getenv("FETCH_STATUSES", "open")     # status_list по умолчанию
    fetch_limit: int = int(os.getenv("FETCH_LIMIT", "20"))        # сколько тикетов за раз
    # Интервал автоопроса HelpDesk поллером (секунды). По умолчанию 20 минут.
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "1200"))
    # Логирование. Пишем и в консоль (видно в tmux вживую), и в файл с ротацией.
    # LOG_FILE пусто → только консоль. Путь создаётся при старте.
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "./logs/agent.log")
    # Ротация: при достижении log_max_bytes файл переоткрывается, хранится
    # log_backups старых копий (agent.log.1 … .N). 10 МБ × 5 = ~50 МБ потолок.
    log_max_bytes: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
    log_backups: int = int(os.getenv("LOG_BACKUPS", "5"))


config = AppConfig()
