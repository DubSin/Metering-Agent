#!/usr/bin/env python3
"""
Скачивание всей базы знаний HelpDeskEddy (API v2) в локальную папку
с сохранением структуры категорий (вложенные подпапки).

Что делает:
  1. Читает все категории (GET /knowledge_base/categories/) и строит дерево по parent_id.
  2. Для каждой категории создаёт подпапку, повторяя иерархию базы знаний.
  3. Читает все статьи (GET /knowledge_base/articles/) постранично и раскладывает
     их по папкам категорий. Тело статьи сохраняется как HTML (по одному файлу
     на язык), вложения (files) — в подпапку <статья>_files/.
  4. Статьи без категории попадают в папку «_uncategorized».

Аутентификация: Basic (email:api_key) — параметры берутся из config / .env:
    HELPDESK_EDDY_BASE_URL, HELPDESK_EDDY_EMAIL, HELPDESK_EDDY_API_KEY

Запуск:
    python -m scripts.download_kb --out ./knowledge_base
    python scripts/download_kb.py --out /path/to/dump --public-only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

# Позволяет запускать как `python scripts/download_kb.py`, так и `-m scripts.download_kb`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv не обязателен
    pass

from config import config  # noqa: E402

# Предпочтительный порядок языков при выборе имени файла/папки.
LANG_PRIORITY = ("ru", "en", "ua", "uk")
PER_PAGE_FALLBACK = 30


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
def api_base() -> str:
    """Сформировать базу API v2 из HELPDESK_EDDY_BASE_URL.

    base_url вида https://support.lar.tech/ru — берём только схему и хост,
    локальный префикс (/ru) к API не относится.
    """
    parts = urlsplit(config.helpdesk_eddy.base_url)
    if not parts.scheme or not parts.netloc:
        raise SystemExit(
            "HELPDESK_EDDY_BASE_URL задан некорректно: "
            f"{config.helpdesk_eddy.base_url!r}"
        )
    return f"{parts.scheme}://{parts.netloc}/api/v2"


def make_client() -> httpx.Client:
    email = config.helpdesk_eddy.email
    api_key = config.helpdesk_eddy.api_key
    if not email or not api_key:
        raise SystemExit(
            "Нужны HELPDESK_EDDY_EMAIL и HELPDESK_EDDY_API_KEY "
            "(Basic-аутентификация API v2). Заполни их в .env."
        )
    return httpx.Client(
        base_url=api_base(),
        auth=(email, api_key),  # Basic email:api_key
        headers={"Accept": "application/json"},
        timeout=60,
        follow_redirects=True,
    )


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, fallback: str) -> str:
    """Превратить строку в безопасное имя файла/папки."""
    name = (name or "").strip()
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(". ")
    if not name:
        return fallback
    return name[:120]


def pick_lang(multilang: dict | None) -> str | None:
    """Выбрать «основной» язык из многоязычного словаря {lang: value}."""
    if not multilang:
        return None
    for lang in LANG_PRIORITY:
        if multilang.get(lang):
            return lang
    # любой непустой
    for lang, val in multilang.items():
        if val:
            return lang
    return next(iter(multilang), None)


def paginate(client: httpx.Client, path: str, params: dict) -> list[dict]:
    """Собрать все объекты со всех страниц. data приходит словарём {id: obj}."""
    items: list[dict] = []
    page = 1
    while True:
        resp = client.get(path, params={**params, "page": page})
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or {}
        if isinstance(data, dict):
            items.extend(data.values())
        elif isinstance(data, list):
            items.extend(data)

        pg = payload.get("pagination") or {}
        total_pages = pg.get("total_pages")
        current = pg.get("current_page", page)
        if not total_pages or current >= total_pages:
            break
        page = current + 1
    return items


# --------------------------------------------------------------------------- #
# Построение дерева категорий и путей
# --------------------------------------------------------------------------- #
def build_category_paths(categories: list[dict]) -> dict[int, Path]:
    """Вернуть {category_id: относительный путь папки}."""
    by_id = {int(c["id"]): c for c in categories}

    def folder_name(cat: dict) -> str:
        title = pick_lang(cat.get("title"))
        text = cat["title"].get(title) if title else None
        return sanitize(text, fallback=f"category-{cat['id']}")

    cache: dict[int, Path] = {}

    def resolve(cid: int, _seen: set[int]) -> Path:
        if cid in cache:
            return cache[cid]
        cat = by_id.get(cid)
        if not cat:
            return Path(f"category-{cid}")
        parent_id = cat.get("parent_id") or 0
        name = folder_name(cat)
        if parent_id and int(parent_id) in by_id and int(parent_id) not in _seen:
            path = resolve(int(parent_id), _seen | {cid}) / name
        else:
            path = Path(name)
        cache[cid] = path
        return path

    for cid in by_id:
        resolve(cid, set())
    return cache


# --------------------------------------------------------------------------- #
# Сохранение статьи
# --------------------------------------------------------------------------- #
def article_filename(article: dict) -> str:
    title_lang = pick_lang(article.get("title"))
    title = article["title"].get(title_lang) if title_lang else None
    base = sanitize(title, fallback=f"article-{article['id']}")
    return f"{article['id']}-{base}"


def html_document(article: dict, lang: str) -> str:
    title = (article.get("title") or {}).get(lang, "")
    body = (article.get("body") or {}).get(lang, "")
    tags = (article.get("tags") or {}).get(lang, []) or []
    meta = {
        "id": article.get("id"),
        "lang": lang,
        "categories": article.get("categories"),
        "access": article.get("access"),
        "date_created": article.get("date_created"),
        "date_updated": article.get("date_updated"),
        "views_count": article.get("views_count"),
        "tags": tags,
    }
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        f"<!-- helpdeskeddy-kb-meta: {json.dumps(meta, ensure_ascii=False)} -->\n"
        "</head>\n<body>\n"
        f"<h1>{title}</h1>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


def download_attachments(
    client: httpx.Client, files: list[dict], dest: Path
) -> int:
    saved = 0
    for i, f in enumerate(files or []):
        url = f.get("url")
        if not url:
            continue
        fname = sanitize(f.get("name", ""), fallback=f"file-{i}")
        dest.mkdir(parents=True, exist_ok=True)
        try:
            r = client.get(url)
            r.raise_for_status()
            (dest / fname).write_bytes(r.content)
            saved += 1
        except httpx.HTTPError as e:
            print(f"    ! вложение не скачано {url}: {e}", file=sys.stderr)
    return saved


def save_article(
    client: httpx.Client,
    article: dict,
    out_root: Path,
    cat_paths: dict[int, Path],
    langs_filter: set[str] | None,
) -> None:
    cats = article.get("categories") or []
    if cats:
        rel = cat_paths.get(int(cats[0]), Path(f"category-{cats[0]}"))
    else:
        rel = Path("_uncategorized")
    folder = out_root / rel
    folder.mkdir(parents=True, exist_ok=True)

    stem = article_filename(article)
    body_langs = [
        lang for lang, val in (article.get("body") or {}).items() if val
    ]
    if langs_filter:
        body_langs = [l for l in body_langs if l in langs_filter]
    if not body_langs:
        body_langs = [pick_lang(article.get("title")) or "ru"]

    primary = pick_lang({l: 1 for l in body_langs}) or body_langs[0]
    for lang in body_langs:
        suffix = "" if lang == primary else f".{lang}"
        (folder / f"{stem}{suffix}.html").write_text(
            html_document(article, lang), encoding="utf-8"
        )

    files = article.get("files") or []
    if files:
        download_attachments(client, files, folder / f"{stem}_files")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default="./knowledge_base",
        help="Папка назначения (по умолчанию ./knowledge_base)",
    )
    ap.add_argument(
        "--public-only",
        action="store_true",
        help="Скачать только публичные категории и статьи",
    )
    ap.add_argument(
        "--lang",
        action="append",
        help="Сохранять только указанные языки (можно повторять: --lang ru --lang en)",
    )
    args = ap.parse_args()

    out_root = Path(args.out).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    langs_filter = set(args.lang) if args.lang else None

    base_params: dict = {}
    if args.public_only:
        base_params["public"] = "true"

    with make_client() as client:
        print(f"API: {api_base()}")
        print("Читаю категории…")
        categories = paginate(client, "/knowledge_base/categories/", base_params)
        cat_paths = build_category_paths(categories)
        print(f"  категорий: {len(categories)}")

        # заранее создаём все папки категорий (даже пустые)
        for rel in cat_paths.values():
            (out_root / rel).mkdir(parents=True, exist_ok=True)

        print("Читаю статьи…")
        articles = paginate(client, "/knowledge_base/articles/", base_params)
        print(f"  статей: {len(articles)}")

        for i, article in enumerate(articles, 1):
            save_article(client, article, out_root, cat_paths, langs_filter)
            if i % 25 == 0 or i == len(articles):
                print(f"  сохранено {i}/{len(articles)}")

    print(f"\nГотово. База знаний выгружена в: {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
