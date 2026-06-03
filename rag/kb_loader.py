"""
Загрузка и нарезка базы знаний (HTML, выгруженной scripts/download_kb.py).

Каждый файл — одна статья:
  - заголовок берём из <title>/<h1>;
  - meta — из комментария <!-- helpdeskeddy-kb-meta: {...} -->;
  - категория — относительный путь папки внутри knowledge_base.

Текст чистим от разметки и режем на чанки по абзацам с перекрытием.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from config import config

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

_META_RE = re.compile(r"helpdeskeddy-kb-meta:\s*(\{.*?\})\s*-->", re.S)


@dataclass
class Chunk:
    uid: str  # стабильный идентификатор «<файл>::<индекс>»
    text: str  # текст для эмбеддинга (заголовок + фрагмент)
    title: str
    source: str  # относительный путь файла внутри базы знаний
    category: str
    chunk_index: int
    meta: dict = field(default_factory=dict)


def _clean_html(soup: "BeautifulSoup") -> str:
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_html_file(path: Path, kb_root: Path) -> tuple[str, str, str, dict]:
    """Вернуть (title, text, category, meta) для одного HTML-файла."""
    from bs4 import BeautifulSoup

    raw = path.read_text(encoding="utf-8", errors="ignore")

    meta: dict = {}
    m = _META_RE.search(raw)
    if m:
        try:
            meta = json.loads(m.group(1))
        except json.JSONDecodeError:
            meta = {}

    soup = BeautifulSoup(raw, "html.parser")
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    elif soup.h1:
        title = soup.h1.get_text().strip()
    title = title or path.stem

    body = soup.body or soup
    text = _clean_html(body)
    # уберём дублирующийся заголовок в начале тела
    if text.startswith(title):
        text = text[len(title):].lstrip("\n")

    category = str(path.relative_to(kb_root).parent)
    return title, text, category, meta


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Жадная нарезка по абзацам до ~size символов, с перекрытием overlap."""
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""

    for p in paras:
        # слишком длинный абзац режем окном
        if len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(size - overlap, 1)
            for s in range(0, len(p), step):
                chunks.append(p[s:s + size])
            continue

        if buf and len(buf) + len(p) + 1 > size:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""
            buf = f"{tail}\n{p}".strip()
        else:
            buf = f"{buf}\n{p}".strip() if buf else p

    if buf:
        chunks.append(buf)
    return chunks


def load_chunks(kb_dir: str | None = None) -> list[Chunk]:
    """Прочитать все *.html из базы знаний и вернуть список чанков."""
    root = Path(kb_dir or config.rag.kb_dir).expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"Папка базы знаний не найдена: {root}")

    size = config.rag.chunk_size
    overlap = config.rag.chunk_overlap

    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*.html")):
        title, text, category, meta = parse_html_file(path, root)
        if not text.strip():
            continue
        rel = str(path.relative_to(root))
        for i, piece in enumerate(chunk_text(text, size, overlap)):
            chunks.append(
                Chunk(
                    uid=f"{rel}::{i}",
                    text=f"{title}\n\n{piece}",
                    title=title,
                    source=rel,
                    category=category,
                    chunk_index=i,
                    meta=meta,
                )
            )
    return chunks
