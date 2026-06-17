"""Тесты справочника клиентов: загрузка xlsx и матчинг по тексту тикета."""
from openpyxl import Workbook

from rag.directory import Directory, load_directory

HEADER = [
    "компания", "Главный заказчик", "почта", "категория", "договор",
    "платформа", "овнер", "активность пу в 2026 году", "количество пу",
    "количество бс", "есть в биллинге", "комментарий",
]
ROWS = [
    ["Водомер", "", "tdi@vodomer.su", "SOHO", "без договора", "прод",
     "mytischi_vodomer", "нет данных", "", "", "", ""],
    ["Алтайкрайэнерго", "", "vi_argunov@altke.ru", "VIP", "без договора", "прод",
     "altke", "да", 253, 1, "", ""],
    ["ООО СНГ-ЕК", "", "mail@asutp66.ru", "SOHO", "без договора", "прод",
     "нет данных", "нет данных", "", "", "", "указки"],
    ["AURORA Mobile Technologies", "", "", "SOHO", "без договора", "прод",
     "aurora@lar.cloud", "нет", 64, 0, "", ""],
]


def _make_xlsx(path):
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in ROWS:
        ws.append(r)
    wb.save(path)


def test_load_skips_rows_without_keys(tmp_path):
    p = tmp_path / "clients.xlsx"
    _make_xlsx(p)
    d = load_directory(str(p))
    # СНГ-ЕК: owner='нет данных', но почта/компания дают ключи → строка остаётся.
    assert len(d) == 4


def test_match_by_owner_slug(tmp_path):
    p = tmp_path / "clients.xlsx"
    _make_xlsx(p)
    d = load_directory(str(p))

    hits = d.match("На объекте altke не выходит на связь ПУ")
    assert len(hits) == 1
    assert hits[0].company == "Алтайкрайэнерго"

    block = d.format_block(hits)
    assert "Справочник клиентов" in block
    assert "Алтайкрайэнерго" in block
    assert "VIP" in block


def test_match_by_company_and_email_domain(tmp_path):
    p = tmp_path / "clients.xlsx"
    _make_xlsx(p)
    d = load_directory(str(p))

    assert d.match("проблема у клиента Водомер")[0].owner == "mytischi_vodomer"
    # домен почты как ключ
    assert d.match("писал с asutp66.ru")[0].company == "ООО СНГ-ЕК"
    # owner-слаг до @ (aurora@lar.cloud → aurora)
    assert d.match("платформа aurora лежит")[0].company == "AURORA Mobile Technologies"


def test_owner_no_data_not_a_needle(tmp_path):
    p = tmp_path / "clients.xlsx"
    _make_xlsx(p)
    d = load_directory(str(p))
    # 'нет данных' не должен стать ключом и матчить всё подряд
    assert d.match("тут просто нет данных по показаниям") == []


def test_word_boundary_avoids_false_positive(tmp_path):
    p = tmp_path / "clients.xlsx"
    _make_xlsx(p)
    d = load_directory(str(p))
    # 'altke' не должен срабатывать внутри другого слова
    assert d.match("слово realtkeeper не про клиента") == []


def test_missing_file_is_empty_directory():
    d = load_directory("F:/nonexistent/clients.xlsx")
    assert isinstance(d, Directory)
    assert len(d) == 0
    assert d.match("altke") == []
    assert d.format_block([]) == ""
