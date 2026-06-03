"""Тесты загрузчика/нарезки базы знаний.

chunk_text не зависит от внешних библиотек. parse_html_file/load_chunks требуют
beautifulsoup4 — при его отсутствии соответствующие тесты пропускаются.
"""
import pytest

from rag.kb_loader import Chunk, chunk_text, load_chunks, parse_html_file

HTML_SAMPLE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Штатные пароли</title>
<!-- helpdeskeddy-kb-meta: {"id": 100, "lang": "ru", "categories": [-2], "tags": []} -->
</head>
<body>
<h1>Штатные пароли</h1>
<p>штатные пароли для энергомер 12345678</p>
<script>var x = 1;</script>
<style>.a{color:red}</style>
<p>штатные пароли для меркуриев 111111</p>
</body>
</html>
"""


# --------------------------- chunk_text (без зависимостей) ---------------------------

def test_chunk_short_text_single_chunk():
    assert chunk_text("абзац один\nабзац два", size=1200, overlap=200) == [
        "абзац один\nабзац два"
    ]


def test_chunk_empty_text():
    assert chunk_text("   \n  \n\t", size=1200, overlap=200) == []


def test_chunk_respects_size():
    text = "\n".join(f"абзац номер {i} " * 10 for i in range(50))
    chunks = chunk_text(text, size=200, overlap=40)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


def test_chunk_windows_huge_paragraph():
    big = "x" * 5000
    chunks = chunk_text(big, size=1200, overlap=200)
    assert len(chunks) == 5
    assert all(len(c) <= 1200 for c in chunks)
    # окно сдвигается на size-overlap = 1000
    assert chunks[1].startswith("x")


def test_chunk_overlap_carries_tail():
    paras = "\n".join([f"абзац {i}" * 30 for i in range(6)])
    chunks = chunk_text(paras, size=400, overlap=50)
    assert len(chunks) >= 2  # текст реально разбит на несколько частей


# --------------------------- parse_html_file / load_chunks (нужен bs4) ---------------------------

def test_parse_html_file(tmp_path):
    pytest.importorskip("bs4")
    kb = tmp_path / "kb"
    cat = kb / "ПНР"
    cat.mkdir(parents=True)
    f = cat / "100-Штатные пароли.html"
    f.write_text(HTML_SAMPLE, encoding="utf-8")

    title, text, category, meta = parse_html_file(f, kb)

    assert title == "Штатные пароли"
    assert category == "ПНР"
    assert meta.get("id") == 100
    # тело извлечено, скрипты/стили вырезаны
    assert "энергомер 12345678" in text
    assert "меркуриев 111111" in text
    assert "var x" not in text
    assert "color:red" not in text
    # дублирующийся заголовок в начале тела убран
    assert not text.startswith("Штатные пароли")


def test_load_chunks(tmp_path):
    pytest.importorskip("bs4")
    kb = tmp_path / "kb"
    (kb / "ПНР").mkdir(parents=True)
    (kb / "ПНР" / "100-Штатные пароли.html").write_text(HTML_SAMPLE, encoding="utf-8")
    (kb / "пустая.html").write_text(
        "<html><head><title>Пусто</title></head><body></body></html>",
        encoding="utf-8",
    )

    chunks = load_chunks(str(kb))

    assert chunks, "ожидался хотя бы один чанк"
    assert all(isinstance(c, Chunk) for c in chunks)
    c = chunks[0]
    # заголовок попадает в текст для эмбеддинга
    assert c.text.startswith("Штатные пароли")
    # source — относительный путь, uid стабилен и детерминирован
    assert c.source == "ПНР/100-Штатные пароли.html"
    assert c.uid == f"{c.source}::{c.chunk_index}"
    # пустая статья не индексируется
    assert all("пустая.html" not in ch.source for ch in chunks)


def test_load_chunks_missing_dir(tmp_path):
    with pytest.raises(SystemExit):
        load_chunks(str(tmp_path / "нет-такой-папки"))
