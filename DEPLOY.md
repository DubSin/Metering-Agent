# Развёртывание Metering Agent на Mac Studio (Apple Silicon, M-Ultra)

Инструкция по установке и запуску проекта на Mac Studio (M-серия Ultra, macOS).
Один процесс `run.py` поднимает поллер HelpDesk (сам тянет новые тикеты раз в
POLL_INTERVAL секунд, только GET) и Telegram-бота ревью (long-polling).
RAG-пайплайн (Qdrant + fastembed) общий. Автозапуск — через `launchd`.

> Раньше тикеты принимались push-webhook'ом на `:8181`. Отказались: HelpDesk у
> нас — внешний SaaS (helpdeskeddy.com), а Mac за NAT/корпоративным VPN, и
> достучаться до него извне облако не может. Поэтому опрашиваем HelpDesk сами
> (pull). Никакой входящий порт/туннель больше не нужен. Файл `webhook.py` в
> репозитории остался — его можно поднять отдельно (`uvicorn webhook:app`), если
> когда-нибудь появится публичный вход.

---

## 1. Почему Mac Ultra тут удобен

- **Памяти много** (64–192 ГБ unified) — эмбеддер `multilingual-e5-large` (~3–4 ГБ)
  и индекс целиком в RAM, без свопа.
- **LLM работает локально.** DeepSeek развёрнут на этой же машине и слушает
  `:8080`. Альтернативно можно поднять Ollama (см. п.5) — в обоих случаях
  генерация идёт локально, без внешних сервисов.
- fastembed считает эмбеддинги на CPU/ANE через onnxruntime — отдельный GPU-стек
  настраивать не нужно.

> Входящий порт не нужен вообще. И поллер, и Telegram-бот делают только
> исходящие соединения (GET к HelpDeskEddy и long-polling к `api.telegram.org`),
> поэтому Mac за NAT/корпоративным VPN работает без проброса портов и туннелей.

---

## 2. Подготовка системы

Homebrew (если ещё нет):

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Python и git:

```bash
brew install python@3.11 git
```

> На Apple Silicon Homebrew живёт в `/opt/homebrew`. Пути ниже даны под это.

Чтобы сервис работал без логина пользователя в GUI, держите Mac включённым и
отключите засыпание (или поставьте `caffeinate`):

```bash
sudo pmset -a sleep 0 disksleep 0       # не уходить в сон
```

---

## 3. Код и зависимости

```bash
cd ~
git clone <URL_РЕПОЗИТОРИЯ> metering-agent
cd metering-agent

python3.11 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

# Playwright (отрисовка отчётов). Если отчёты на этом узле не нужны — пропустите.
playwright install chromium
```

---

## 4. Конфигурация (`.env`)

```bash
cp .env.example .env
nano .env        # или: open -e .env
```

Заполните обязательные секреты (остальное — из `.env.example`):

```ini
# HelpDeskEddy — Basic auth (email:api_key)
HELPDESK_EDDY_BASE_URL=https://support.lar.tech
HELPDESK_EDDY_EMAIL=operator@example.com
HELPDESK_EDDY_API_KEY=<ключ_из_профиля_HDE>

# LLM-провайдер RAG (deepseek | ollama) — см. п.5
LLM_PROVIDER=deepseek
DEEPSEEK_BASE_URL=http://chatbot.lar.tech:8080/v1
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-flash

# Telegram-бот ревью
TELEGRAM_BOT_TOKEN=<токен_от_BotFather>
TELEGRAM_CHAT_ID=<id_группы_для_ревью>   # напр. -1001234567890
TELEGRAM_PROXY=                          # заполнить, если Telegram заблокирован
TELEGRAM_ALLOWED_USERS=                  # белый список; пусто = разрешено всем

# Векторное хранилище и кэш модели
QDRANT_URL=./data/qdrant
EMBED_MODEL=intfloat/multilingual-e5-large
KB_DIR=./knowledge_base
FASTEMBED_CACHE_PATH=./models            # веса эмбеддера (~2 ГБ)
```

Закройте права на секреты: `chmod 600 .env`.

Каталоги `data/`, `models/`, `knowledge_base/` в git не хранятся — создаются ниже.

---

## 5. LLM-провайдер: локальный DeepSeek или Ollama

`LLM_PROVIDER` выбирает движок генерации в RAG. Оба варианта работают локально —
наружу за LLM ходить не нужно:

- **`deepseek` (по умолчанию)** — отдельный, независимо работающий
  OpenAI-совместимый сервер DeepSeek, слушает `:8080`
  (`DEEPSEEK_BASE_URL=http://chatbot.lar.tech:8080/v1` — имя резолвится на тот же
  узел). Им управляет не наш агент; со стороны агента это просто HTTP-эндпоинт.
  Именно DeepSeek занимает 8080, поэтому webhook поднят на 8181.
- **`ollama` — альтернатива на Mac Ultra** (если DeepSeek недоступен):
  ```bash
  brew install ollama
  brew services start ollama          # автозапуск Ollama как сервиса
  ollama pull qwen3:4b-instruct       # или модель помощнее — Ultra потянет
  ```
  В `.env`:
  ```ini
  LLM_PROVIDER=ollama
  OLLAMA_BASE_URL=http://localhost:11434/v1
  OLLAMA_MODEL=qwen3:4b-instruct
  ```

---

## 6. Выбор хранилища Qdrant

`QDRANT_URL` имеет три формы — выберите одну:

- **`./data/qdrant` (рекомендуется)** — встроенное файловое хранилище, отдельный
  сервер не нужен (движок внутри `qdrant-client`). Ограничение: **один процесс за
  раз** (файловая блокировка) → индексацию делаем при ОСТАНОВЛЕННОМ сервисе (п.10).
- **`http://localhost:6333`** — серверный Qdrant, если хотите переиндексировать без
  остановки приложения:
  ```bash
  brew install qdrant && brew services start qdrant
  # или Docker Desktop:
  # docker run -d --name qdrant --restart unless-stopped -p 6333:6333 \
  #   -v ~/qdrant-storage:/qdrant/storage qdrant/qdrant
  ```
  Тогда `QDRANT_URL=http://localhost:6333` (+ `QDRANT_API_KEY`, если включали).
- **`:memory:`** — без персистентности, только для тестов.

---

## 7. База знаний и индексация (первый запуск)

```bash
source .venv/bin/activate

# 1) Скачать базу знаний из HelpDeskEddy в ./knowledge_base
python -m scripts.download_kb --out ./knowledge_base

# 2) Построить индекс (первый раз качает эмбеддер ~2 ГБ в ./models)
python -m rag.index --recreate
```

> При файловом Qdrant индексируйте, когда сервис приложения **остановлен**.

Проверка одним прогоном:

```bash
python -m rag.ask "как массово внести метки в ЮС"
```

---

## 8. Автозапуск через launchd

Создайте `~/Library/LaunchAgents/tech.lar.metering-agent.plist`
(подставьте своё имя пользователя вместо `<USER>`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>tech.lar.metering-agent</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/<USER>/metering-agent/.venv/bin/python</string>
        <string>/Users/<USER>/metering-agent/run.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/<USER>/metering-agent</string>

    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>

    <key>StandardOutPath</key>
    <string>/Users/<USER>/metering-agent/logs/agent.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/<USER>/metering-agent/logs/agent.err.log</string>
</dict>
</plist>
```

`run.py` сам подхватывает `.env` через `python-dotenv` — отдельно переменные в plist
прописывать не нужно. Подготовьте папку логов и загрузите агент:

```bash
mkdir -p ~/metering-agent/logs
launchctl load  ~/Library/LaunchAgents/tech.lar.metering-agent.plist
launchctl list | grep metering          # проверить, что запущен
tail -f ~/metering-agent/logs/agent.err.log   # логи (logging пишет в stderr)
```

Управление:

```bash
launchctl unload ~/Library/LaunchAgents/tech.lar.metering-agent.plist   # остановить
launchctl load   ~/Library/LaunchAgents/tech.lar.metering-agent.plist   # запустить
# перезапуск = unload + load
```

> **LaunchAgent vs LaunchDaemon.** Агент (`~/Library/LaunchAgents`) работает в сессии
> пользователя — проще, но требует, чтобы пользователь был залогинен (или включён
> авто-логин). Для работы без логина положите тот же plist в
> `/Library/LaunchDaemons/`, владельцем `root:wheel`, и грузите через
> `sudo launchctl load`. Для одиночного Mac Studio с авто-логином хватает агента.

---

## 9. Проверка работоспособности

```bash
source .venv/bin/activate

# один проход поллера вручную (подтянет открытые тикеты и пришлёт новые в группу)
python -m poller --once

# RAG отдельно из консоли
python -m rag.ask "как массово внести метки в ЮС"
```

Telegram-бот: в группе `TELEGRAM_CHAT_ID` отправьте `/help`, затем `/fetch` —
подтянет открытые тикеты из HelpDesk на ревью. Дальше поллер делает то же самое
автоматически каждые `POLL_INTERVAL` секунд (по умолчанию 20 минут); уже
отправленные тикеты повторно не рассылаются (дедуп по SQLite).

> Поллер опрашивает только статусы из `FETCH_STATUSES` (по умолчанию `open`) и
> не более `FETCH_LIMIT` тикетов за проход.

---

## 10. Сетевой доступ (поллер — только исходящие)

Входящий доступ к Mac не требуется: поллер сам ходит за тикетами. Достаточно,
чтобы с Mac открывались **исходящие** соединения к двум адресам:

- `https://support.lar.tech` (HelpDeskEddy API, GET) — проверка:
  ```bash
  curl -I https://support.lar.tech        # ответ сервера = доступ есть
  ```
- `https://api.telegram.org` (Telegram-бот). Если у провайдера/VPN он закрыт —
  пропишите `TELEGRAM_PROXY=socks5://…` в `.env` (прокси только для Telegram).

Проброс портов, туннели и reverse-proxy больше не нужны — это и было главным
смыслом перехода с webhook на поллер для Mac за NAT/корпоративным VPN.

---

## 11. Обновление и обслуживание

**Обновить код:**

```bash
cd ~/metering-agent
launchctl unload ~/Library/LaunchAgents/tech.lar.metering-agent.plist
git pull
source .venv/bin/activate && pip install -r requirements.txt
launchctl load ~/Library/LaunchAgents/tech.lar.metering-agent.plist
```

**Переиндексировать базу знаний** (вышли новые статьи в HelpDesk):

```bash
# Файловый Qdrant: сначала останавливаем сервис
launchctl unload ~/Library/LaunchAgents/tech.lar.metering-agent.plist
cd ~/metering-agent && source .venv/bin/activate
python -m scripts.download_kb --out ./knowledge_base
python -m rag.index --recreate
launchctl load ~/Library/LaunchAgents/tech.lar.metering-agent.plist
# (серверный Qdrant останавливать сервис не требует)
```

**Бэкап** — копируйте регулярно (Time Machine + ручной дамп SQLite):
- `data/feedback.sqlite3` — датасет решений операторов (главная ценность);
- `data/qdrant/` — индекс (опционально, восстанавливается переиндексацией);
- `.env` — секреты.

```bash
sqlite3 data/feedback.sqlite3 ".backup '$HOME/Backup/feedback-$(date +%F).sqlite3'"
```

**Экспорт датасета для дообучения:**

```bash
python -m scripts.export_dataset
```

---

## 12. Типичные проблемы

| Симптом | Причина / решение |
|---------|-------------------|
| После перезагрузки Mac сервис не поднялся | Нет авто-логина пользователя → используйте LaunchDaemon (п.8) или включите автоматический вход |
| Сервис «засыпает» ночью | Mac уходит в сон → `sudo pmset -a sleep 0` или запуск под `caffeinate` |
| Бот не отвечает, таймауты к `api.telegram.org` | Telegram заблокирован у провайдера → `TELEGRAM_PROXY=socks5://...` |
| `database is locked` / ошибка Qdrant при старте | Файловый Qdrant запущен дважды. Индексируйте при остановленном сервисе или перейдите на серверный Qdrant |
| Долгий первый старт, качает ~2 ГБ | fastembed тянет модель в `FASTEMBED_CACHE_PATH`. Один раз, дальше из кэша |
| `401`/`403` от HelpDesk | Неверные `HELPDESK_EDDY_EMAIL`/`HELPDESK_EDDY_API_KEY` (Basic = email:api_key) |
| RAG отвечает «нет данных» | Не выполнена индексация или пустой `knowledge_base/` |
| Поллер молчит, новых тикетов нет в группе | Проверьте `FETCH_STATUSES`/`FETCH_LIMIT`, доступ `curl -I https://support.lar.tech` и логи; первый проход — через `POLL_INTERVAL` после старта (или `python -m poller --once`) |
| Тикет не приходит повторно | Так и задумано: дедуп по SQLite, уже отправленные на ревью не рассылаются снова |

---

## Шпаргалка команд

```bash
# управление сервисом
launchctl load   ~/Library/LaunchAgents/tech.lar.metering-agent.plist   # старт
launchctl unload ~/Library/LaunchAgents/tech.lar.metering-agent.plist   # стоп
tail -f ~/metering-agent/logs/agent.err.log                             # логи

python -m poller --once                   # один проход поллера вручную
python -m scripts.download_kb             # обновить базу знаний
python -m rag.index --recreate            # переиндексировать
python -m rag.ask "вопрос"                # проверить RAG из консоли
python -m scripts.export_dataset          # выгрузить датасет
```
