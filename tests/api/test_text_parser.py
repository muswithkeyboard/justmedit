"""Тесты разбора вставленного текста бланка (deficitlens_core/io/text_parser.py). Значения придуманы."""
from __future__ import annotations

import helpers  # noqa: F401 — добавляет src в sys.path

from deficitlens_core.config import load_config
from deficitlens_core.io.text_parser import parse_lab_text

TEXT = """Иванов Иван Иванович
Дата взятия: 12.03.2026
Общий анализ крови
Гемоглобин 124 г/л
Ферритин: 9,0 мкг/л (норма 10–120)
MCV 82 фл
Hb - 124
Эритроциты (RBC) 4,59 х10^12/л 3.8-5.1
Нейтрофилы 55 %
RDW-SD 42 фл
Гликированный гемоглобин 5,6 %
Фолиевая кислота 9 нмоль/л
СРБ < 5 мг/л
"""


def _parse(text: str = TEXT) -> dict:
    return parse_lab_text(text, load_config())


def test_values_units_and_decimal_comma():
    v = _parse()["values"]
    assert v["hemoglobin"] == {"value": 124.0, "unit": "г/л"}
    assert v["ferritin"]["value"] == 9.0 and v["ferritin"]["unit"] == "мкг/л"      # интервал в скобках не взят
    assert v["MCV"] == {"value": 82.0, "unit": "фл"}
    assert v["RBC"]["value"] == 4.59 and v["RBC"]["unit"] in ("10^12/л", "x10^12/l")  # интервал после значения не взят
    assert v["folate"] == {"value": 9.0, "unit": "нмоль/л"}                         # единица не пересчитывается здесь
    assert v["CRP"]["value"] == 5.0


def test_alias_dash_and_duplicate():
    out = _parse("Hb - 131\nгемоглобин 99 г/л\n")
    assert out["values"]["hemoglobin"]["value"] == 131.0
    assert any("несколько раз" in n for n in out["notes"])


def test_garbage_lines_and_no_personal_data():
    out = _parse()
    assert "RDW" not in out["values"], "RDW-SD — другой показатель, не RDW (%)"
    assert set(out["values"]) == {"hemoglobin", "ferritin", "MCV", "RBC", "folate", "CRP"}
    joined = " ".join(out["unrecognized"] + out["notes"])
    assert "Иванов" not in joined and "12.03.2026" not in joined, "ФИО и даты не возвращаются"
    # Текст строк бланка не возвращается: только номер строки и распознанные части (этап 4а).
    assert "строка 9: название показателя не распознано" in out["unrecognized"]           # Нейтрофилы 55 %
    # П2: название сопоставляется целиком, без поиска подстроки — «Гликированный гемоглобин» не гемоглобин уже
    # по названию (прежде строка находилась как «Гемоглобин» и отсекалась только жёстким диапазоном 5,6 г/л).
    assert "строка 11: название показателя не распознано" in out["unrecognized"], "5,6 % — не гемоглобин в г/л"
    assert "hemoglobin" in out["values"] and out["values"]["hemoglobin"]["value"] == 124.0
    assert "Нейтрофилы" not in joined and "Гликированный" not in joined
    ext = parse_lab_text(TEXT, load_config(), extended=True)       # ответ /parse: известные, но не используемые
    assert {"Нейтрофилы", "RDW-SD", "Гликированный гемоглобин (HbA1c)"} <= set(ext["ignored"]), ext["ignored"]
    assert not any(u.startswith(("строка 9:", "строка 10:", "строка 11:")) for u in ext["unrecognized"]), ext
    assert all(len(u) <= 60 for u in out["unrecognized"])


def test_unrecognized_is_truncated_and_sex_age_not_extracted():
    out = _parse("Какой-то очень длинный неизвестный показатель с числом внутри строки и хвостом 12,5 ед/л доп. текст\n"
                 "Пол: женский, возраст 34 года\n")
    assert out["values"] == {}
    assert out["unrecognized"] and all(len(u) <= 60 for u in out["unrecognized"])
    assert set(out) == {"values", "unrecognized", "notes"}, "пол и возраст из текста не извлекаются"


def test_names_from_config_without_case():
    out = _parse("СЫВОРОТОЧНОЕ ЖЕЛЕЗО 5.5\nvitamin_b12 230 пг/мл\nРет-he 28\nRet-He 28 пг\nТТГ 2,1 мкМЕ/мл\n")
    v = out["values"]
    assert v["serum_iron"] == {"value": 5.5, "unit": None}
    assert v["vitamin_B12"]["value"] == 230.0
    assert v["Ret_He"]["value"] == 28.0
    assert v["TSH"]["value"] == 2.1 and v["TSH"]["unit"] is not None


def test_bad_input():
    try:
        parse_lab_text("x" * 30000, load_config())
    except ValueError:
        pass
    else:
        raise AssertionError("слишком длинный текст должен отклоняться")
    assert _parse("")["values"] == {}


def test_cyrillic_latin_lookalikes_in_names():
    """Этап 7: «Витамин В12» с русской «В» и «МСН» русскими буквами (так часто читает OCR и пишут бланки)."""
    text = "Витамин В12 210 пг/мл\nМСН 25,4 пг\nМСV 78 фл\nАЛТ 20 Ед/л\n"
    r = parse_lab_text(text, load_config(), line_numbers=True)
    v = r["values"]
    assert v["vitamin_B12"] == {"value": 210.0, "unit": "пг/мл"}, r
    assert v["MCH"]["value"] == 25.4 and v["MCV"]["value"] == 78.0, r
    assert r["lines"] == {"vitamin_B12": 1, "MCH": 2, "MCV": 3}, r
    assert "lines" not in parse_lab_text(text, load_config())


def test_text_path_returns_analysis_date_like_pdf():
    """Поле date — во всех путях /parse: вставленный текст (/ui/parse и /api/v1/parse с JSON), TXT-файл. Подпись
    и дата на соседних строках (так копируется строка таблицы из двух ячеек) — тоже дата анализа."""
    import io

    from deficitlens_api import service
    from deficitlens_api.app import create_app
    from deficitlens_api.settings import Settings

    text = "Пациент: Проверочная А.С., дата рождения 07.02.1985\nДата взятия: 01.09.2026\nГемоглобин 118 г/л 120-140"
    assert service.parse_text(text)["date"] == "2026-09-01"
    txt = service.parse_file(text.encode("cp1251"), "бланк.txt")
    assert txt["date"] == "2026-09-01" and txt["source"] == {"format": "txt"}
    try:
        from starlette.testclient import TestClient  # нужен httpx; без него — только проверки сервиса выше
    except ImportError:
        TestClient = None
    if TestClient is not None:
        c = TestClient(create_app(Settings(api_keys=("txt-key",), ui_rate_per_min=10000)))
        r = c.post("/ui/parse", json={"text": text})
        assert r.status_code == 200 and r.json()["date"] == "2026-09-01", r.text
        r = c.post("/api/v1/parse", json={"text": text}, headers={"X-API-Key": "txt-key"})
        assert r.status_code == 200 and r.json()["date"] == "2026-09-01", r.text
        r = c.post("/ui/parse", files={"file": ("бланк.txt", io.BytesIO(text.encode("utf-8")), "text/plain")})
        assert r.status_code == 200 and r.json()["date"] == "2026-09-01", r.text
    for split in ("Дата взятия:\n01.09.2026 08:40\nГемоглобин 118 г/л", "Дата взятия\n\n1 сентября 2026\nHb 118"):
        assert service.parse_text(split)["date"] == "2026-09-01", split
    assert service.parse_text("Дата рождения:\n12.03.2024\nГемоглобин 118 г/л")["date"] is None


def test_report_line_with_name_but_no_number():
    """Фото: «Ферритин мкг/л» (значение не прочиталось) — не теряется молча, а попадает в unrecognized."""
    text = "Иванова Мария\nГемоглобин 118 г/л\nФерритин FFs мкг/л\nОбщий анализ крови\n"
    assert parse_lab_text(text, load_config())["unrecognized"] == []          # текст и PDF — как раньше
    r = parse_lab_text(text, load_config(), report_no_number=True)
    assert r["unrecognized"] == ["строка 3: Ферритин — нет числа"], r
    assert list(r["values"]) == ["hemoglobin"]
