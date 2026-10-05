"""П2: разбор бланка любой лаборатории — названия (строго, без подстроки), единицы, нормы, дата, строки текста
разных порядков и таблицы PDF по заголовку и по содержимому. Все значения и «ПДн» придуманы."""
from __future__ import annotations

import helpers  # noqa: F401 — добавляет src в sys.path

from deficitlens_core.config import load_config
from deficitlens_core.io import lab_blank as lb
from deficitlens_core.io.lab_tables import parse_tables
from deficitlens_core.io.text_parser import parse_lab_text


def _cfg():
    return load_config()


def _match(name: str):
    t = lb.match_name(name, lb.name_index(_cfg()))
    return None if t is None else (t.code if t.kind == "analyte" else "ignored:" + t.code)


def _ext(text: str, **kw) -> dict:
    return parse_lab_text(text, _cfg(), extended=True, **kw)


# ---- справочник ---------------------------------------------------------------------------------------------
def test_synonyms_are_unique_across_analytes_and_not_used():
    """Один нормализованный синоним — один показатель; известные неиспользуемые не пересекаются с используемыми."""
    seen: dict[str, lb.Target] = {}
    clashes = []
    for key, target, spelling in lb.synonym_table(_cfg()):
        if key in seen and seen[key] != target:
            clashes.append((spelling, seen[key], target))
        seen.setdefault(key, target)
    assert not clashes, clashes
    assert len(lb.not_used_entries()) >= 20


def test_names_strict_no_substring():
    """Полное совпадение или аббревиатура в скобках; подстрока не ищется."""
    assert _match("Средний объем тромбоцитов в крови") == "ignored:Средний объём тромбоцитов (MPV)"
    assert _match("Средний объём тромбоцитов (MPV)") == "ignored:Средний объём тромбоцитов (MPV)"
    assert _match("Тромбоциты, средний объем (MPV)") == "ignored:Средний объём тромбоцитов (MPV)"
    assert _match("Абсолютное количество нейтрофилов") == "ignored:Нейтрофилы"
    assert _match("Тромбокрит") == "ignored:Тромбокрит (PCT)"
    assert _match("Общий объем тромбоцитов в крови (тромбокрит, PСT)") == "ignored:Тромбокрит (PCT)"  # «С» русская
    assert _match("Ширина распределения тромбоцитов по объему") == "ignored:Ширина распределения тромбоцитов (PDW)"
    assert _match("Ширина распределения эритроцитов (RDW-SD)") == "ignored:RDW-SD"   # аббревиатура точнее
    assert _match("Гликированный гемоглобин") == "ignored:Гликированный гемоглобин (HbA1c)"
    assert _match("Гемоглобин (HbA1c)") == "ignored:Гликированный гемоглобин (HbA1c)"
    assert _match("Иванов гемоглобин") is None and _match("Гемоглобин пациента") is None
    assert _match("Нейтрофилы, %") == "ignored:Нейтрофилы"


def test_names_of_different_labs():
    cases = {
        "Гемоглобин общий": "hemoglobin", "ГЕМОГЛОБИН (HGB)": "hemoglobin", "Гемоглобин HGB": "hemoglobin",
        "Количество эритроцитов": "RBC", "Эритроциты (RBC)": "RBC", "Гематокрит (HCT)": "hematocrit",
        "Средний объём эритроцита": "MCV", "Среднее содержание гемоглобина в эритроците": "MCH",
        "Средняя концентрация гемоглобина в эритроците": "MCHC",
        "Ширина распределения эритроцитов по объему": "RDW", "RDW-CV": "RDW", "Количество тромбоцитов": "platelets",
        "Количество лейкоцитов": "WBC", "Скорость оседания эритроцитов (по Вестергрену)": "ESR",
        "СОЭ (по Панченкову)": "ESR", "Ферритин сыворотки": "ferritin", "Железо сывороточное": "serum_iron",
        "Железо в сыворотке": "serum_iron", "ОЖСС": "TIBC", "Общая железосвязывающая способность сыворотки": "TIBC",
        "Трансферрин": "transferrin", "Насыщение трансферрина железом": "TSAT",
        "Витамин B12 (цианокобаламин)": "vitamin_B12", "Витамин В12": "vitamin_B12",   # «В» русская
        "Фолиевая кислота": "folate", "Фолаты сыворотки": "folate",
        "С-реактивный белок (высокочувствительный)": "CRP", "C-реактивный белок": "CRP",   # «C» латинская
        "Креатинин": "creatinine", "ТТГ": "TSH", "Тиреотропный гормон (ТТГ)": "TSH", "ЛДГ": "LDH",
        "Лактатдегидрогеназа": "LDH", "Билирубин непрямой": "indirect_bilirubin", "Гаптоглобин": "haptoglobin",
        "Ретикулоциты": "reticulocytes", "Церулоплазмин": "ceruloplasmin", "Медь": "copper",
        "Витамин B6": "vitamin_B6", "Гомоцистеин": "homocysteine", "Альбумин": "albumin",
        "Скорость оседания эритроцитов (по": "ESR",            # незакрытая скобка при переносе ячейки
    }
    wrong = {n: (_match(n), code) for n, code in cases.items() if _match(n) != code}
    assert not wrong, wrong


# ---- единицы, нормы, дата -------------------------------------------------------------------------------------
def test_unit_forms_normalized_and_mapped():
    for form in ("×10⁹/л", "x10^9/l", "10*9/л", "10e9/л", "109/л", "10^9 /л", "х10^9/л", "10⁹/л"):
        assert lb.unit_norm(form) in ("10^9/л", "10^9/l"), form
    wbc = _cfg().analytes["WBC"]
    assert lb.unit_for_cell("×10⁹/л", wbc)[0] in ("10^9/л", "10^9/l")
    assert lb.unit_for_cell("10*9/л", wbc)[0] in ("10^9/л", "10^9/l")
    assert lb.unit_for_cell("г/дл", _cfg().analytes["hemoglobin"]) == ("г/дл", False)
    assert lb.unit_for_cell("ммоль/л", _cfg().analytes["hemoglobin"]) == (None, True)
    assert lb.unit_for_cell("-", wbc) == (None, False)


def test_reference_range_forms():
    def rng(s):
        found = lb.find_range(s)
        return None if found is None else found[0]
    assert rng("120-150") == (120.0, 150.0)
    assert rng("120 – 150") == (120.0, 150.0)
    assert rng("3,9…5,2") == (3.9, 5.2)
    assert rng("от 120 до 150") == (120.0, 150.0)
    assert rng("< 5") == (None, 5.0) and rng("до 5") == (None, 5.0) and rng("менее 5") == (None, 5.0)
    assert rng("> 30") == (30.0, None) and rng("более 30") == (30.0, None)
    assert rng("112-233-445 95") is None, "номер документа — не норма"
    assert rng("+7 912 345-67-89") is None, "телефон — не норма"


def test_date_priority_and_no_birth_date():
    assert lb.find_date(["Дата: 16.04.2025"]) == "2025-04-16"
    assert lb.find_date(["Дата регистрации: 03.03.2026    Дата взятия: 02.03.2026"]) == "2026-03-02"
    assert lb.find_date(["Дата выдачи: 10.03.2026", "Дата выполнения: 09.03.2026"]) == "2026-03-09"
    assert lb.find_date(["Дата взятия биоматериала: 05.02.2026 08:40", "Дата выполнения: 06.02.2026"]) == "2026-02-05"
    assert lb.find_date(["Дата рождения: 07.02.1985"]) is None
    assert lb.find_date(["Дата poждения: 07.02.1995"]) is None, "латинские p, o после распознавания — тоже рождение"
    assert lb.find_date(["Дата рождения: 07.02.1985 Дата: 12.03.26"]) == "2026-03-12"
    assert lb.find_date(["Дата взятия 5 марта 2026 г."]) == "2026-03-05"
    assert lb.find_date(["Пациент 12.03.2026", "Тел. 01.02.2026"]) is None, "дата без подписи не берётся"


# ---- строки текста ---------------------------------------------------------------------------------------------
def test_line_orders_flags_and_ranges():
    text = "\n".join([
        "Гемоглобин (HGB) 112 г/л 117 - 160",                 # название (аббр.) значение единица норма
        "Эритроциты 4,05 3,9-5,2 10^12/л",                    # значение норма единица
        "Ферритин ↓ 8,5 мкг/л 10 - 120",                      # стрелка перед значением
        "Железо сывороточное 7,1 * мкмоль/л 9,0 – 30,4",
        "ОЖСС 78,2 H мкмоль/л 45,3 - 77,1",
        "С-реактивный белок 3,2 мг/л < 5",
        "Гомоцистеин 14,2 ↑ мкмоль/л до 10",
        "Фолиевая кислота 3,1 нг/мл более 3,0",
        "Лейкоциты 4,0-9,0 6,1 10*9/л",                       # норма раньше значения — значение следующее число
        "Тромбоциты 251 Ниже нормы 10^9/л 160-370",
        "Скорость оседания эритроцитов (по Вестергрену) 14 2-20 - - мм/ч",
    ])
    r = _ext(text)
    v = {k: (x["value"], x["unit"]) for k, x in r["values"].items()}
    assert v["hemoglobin"] == (112.0, "г/л")
    assert v["RBC"][0] == 4.05 and v["RBC"][1] in ("10^12/л", "10^12/l")
    assert v["ferritin"] == (8.5, "мкг/л") and v["serum_iron"] == (7.1, "мкмоль/л") and v["TIBC"][0] == 78.2
    assert v["CRP"] == (3.2, "мг/л") and v["homocysteine"] == (14.2, "мкмоль/л") and v["folate"] == (3.1, "нг/мл")
    assert v["WBC"][0] == 6.1 and v["WBC"][1] in ("10^9/л", "10^9/l")
    assert v["platelets"][0] == 251.0 and v["ESR"] == (14.0, "мм/ч")
    rr = r["reference_ranges"]
    assert rr["hemoglobin"] == {"low": 117.0, "high": 160.0, "unit": "г/л"}
    assert (rr["RBC"]["low"], rr["RBC"]["high"]) == (3.9, 5.2)
    assert (rr["CRP"]["low"], rr["CRP"]["high"]) == (None, 5.0)
    assert (rr["homocysteine"]["low"], rr["homocysteine"]["high"]) == (None, 10.0)
    assert (rr["folate"]["low"], rr["folate"]["high"]) == (3.0, None)
    assert (rr["WBC"]["low"], rr["WBC"]["high"]) == (4.0, 9.0)
    assert (rr["platelets"]["low"], rr["platelets"]["high"]) == (160.0, 370.0)
    assert r["unrecognized"] == [] and r["ignored"] == []


def test_multiline_names_are_glued():
    text = "\n".join([
        "Насыщение трансферрина",              # название выше строки значения
        "железом 9 % 20 - 50",
        "Среднее содержание гемоглобина в 27 26-34 - - пг",
        "эритроците",                          # окончание названия ниже строки значения
        "Средняя концентрация гемоглобина",    # название разорвано вокруг строки значения (PDF)
        "334 300-380 - - г/л",
        "в эритроците",
        "Абсолютное количество",
        "2.44 1.8-7.7 - - 10^9/л",
        "нейтрофилов",
        "Количество лейкоцитов 4.23 4-9 - - 10^9/л",
    ])
    r = _ext(text)
    assert {k: x["value"] for k, x in r["values"].items()} == {"TSAT": 9.0, "MCH": 27.0, "MCHC": 334.0, "WBC": 4.23}
    assert r["ignored"] == ["Нейтрофилы"] and r["unrecognized"] == []


def test_reference_range_converted_and_unknown_unit_not_converted():
    r = _ext("Гемоглобин 11,2 г/дл 11,7 - 16,0\nЖелезо сывороточное 45 мкг/дл 50 - 170\n"
             "Ферритин 30 ммоль/л 10 - 120\nВитамин B12 300 200 - 900\nMCV 82 фл 100 - 900")
    assert r["values"]["MCV"]["value"] == 82.0 and "MCV" not in r["reference_ranges"], "граница вне hard — не норма"
    assert r["values"]["hemoglobin"] == {"value": 11.2, "unit": "г/дл"}             # значение не пересчитано
    assert r["reference_ranges"]["hemoglobin"] == {"low": 117.0, "high": 160.0, "unit": "г/л"}
    assert r["reference_ranges"]["serum_iron"] == {"low": 8.95, "high": 30.43, "unit": "мкмоль/л"}
    assert r["values"]["ferritin"] == {"value": 30.0, "unit": None}                  # неизвестная единица
    assert "ferritin" not in r["reference_ranges"], "норму в неизвестной единице не пересчитываем молча"
    assert any("Ферритин: единица не распознана" in n for n in r["notes"])
    assert "vitamin_B12" not in r["reference_ranges"], "у B12 несколько единиц: без единицы норму не берём"


def test_default_mode_keeps_old_shape():
    r = parse_lab_text("Нейтрофилы 55 %\nГемоглобин 120 г/л 120-150\nДата взятия: 01.02.2026", _cfg())
    assert set(r) == {"values", "unrecognized", "notes"}
    assert r["unrecognized"] == ["строка 1: название показателя не распознано"]
    r = _ext("Нейтрофилы 55 %\nГемоглобин 120 г/л 120-150\nДата взятия: 01.02.2026")
    assert r["ignored"] == ["Нейтрофилы"] and r["unrecognized"] == [] and r["date"] == "2026-02-01"


# ---- таблицы PDF ---------------------------------------------------------------------------------------------
def test_tables_by_header_any_order_and_continuation():
    t1 = [["Тест", "Результат", "Норма", "Отклонение", "Критичность\nотклонения", "Ед. изм."],
          ["Гемоглобин общий", "110", "120-150", "отклонение от\nнормы", "Ниже нормы\n(числовой результат)", "г/л"],
          ["Среднее содержание гемоглобина в\nэритроците", "27", "26-34", "-", "-", "пг"],
          ["Средний объем тромбоцитов в\nкрови", "11.9", "3.6-12", "-", "-", "фл"]]
    t2 = [["Абсолютное количество\nнейтрофилов", "2.44", "1.8-7.7", "-", "-", "10^9/л"],   # без заголовка
          ["Количество лейкоцитов", "4.23", "4-9", "-", "-", "10^9/л"]]
    t3 = [["Исследование", "Результат", "Единицы", "Референсные значения"],
          ["Ферритин", "12", "нг/мл", "10 - 120"],
          ["Исследование", "Результат", "Единицы", "Референсные значения"],               # заголовок повторён
          ["Железо сывороточное", "45", "мкг/дл", "50 - 170"]]
    r = _ext("Дата: 16.04.2025", tables=[t1, t2, t3])
    v = {k: (x["value"], x["unit"]) for k, x in r["values"].items()}
    assert v == {"hemoglobin": (110.0, "г/л"), "MCH": (27.0, "пг"), "WBC": (4.23, "10^9/л"),
                 "ferritin": (12.0, "нг/мл"), "serum_iron": (45.0, "мкг/дл")}
    assert "MCV" not in v and r["ignored"] == ["Средний объём тромбоцитов (MPV)", "Нейтрофилы"]
    assert r["reference_ranges"]["serum_iron"] == {"low": 8.95, "high": 30.43, "unit": "мкмоль/л"}
    assert r["date"] == "2025-04-16"


def test_tables_without_header_by_content_and_wrapped_rows():
    t = [["Гемоглобин", "118", "г/л", "120 - 140"],
         ["Средняя концентрация гемоглобина", "", "", ""],      # название разорвано на две строки таблицы
         ["в эритроците", "330", "г/л", "300 - 380"],
         ["Тромбокрит", "0,21", "%", "0,15 - 0,40"],
         ["Пациент Проверочная А.С.", "", "", ""]]
    r = _ext("", tables=[t])
    assert {k: x["value"] for k, x in r["values"].items()} == {"hemoglobin": 118.0, "MCHC": 330.0}
    assert r["reference_ranges"]["MCHC"]["low"] == 300.0 and r["ignored"] == ["Тромбокрит (PCT)"]
    # Таблица в один столбец — разбирается как строки текста.
    r = _ext("", tables=[[["Гемоглобин 121 г/л 120-140"], ["Ферритин 20 мкг/л 10-120"]]])
    assert set(r["values"]) == {"hemoglobin", "ferritin"}


def test_table_rows_do_not_leak_text():
    cfg = _cfg()
    col = lb.Collector(cfg, extended=True)
    leftover, _ = parse_tables([[["Тест", "Результат", "Ед."], ["Иванов Иван 8 912 345 67 89", "124", "г/л"],
                                 ["Гемоглобин", "124", "г/л"]]], col, lb.name_index(cfg))
    out = col.result()
    assert leftover == []
    assert out["values"] == {"hemoglobin": {"value": 124.0, "unit": "г/л"}}
    assert out["unrecognized"] == ["строка таблицы 1: название показателя не распознано"]
    assert "Иванов" not in str(out) and "912" not in str(out)


# ---- ревизия разбора бланка: H1 числа, H2 прошлые результаты, H3 моча, H4 даты, M2 RDW-SD, СОЭ -----------------
def _vals(r: dict) -> dict:
    return {k: (x["value"], x["unit"]) for k, x in r["values"].items()}


def test_number_reading_thousands_and_ambiguous():
    """H1: «1 210» — одно число; «141 102» — два результата; «45 108» и «1.210» — неоднозначно."""
    cfg = _cfg().analytes
    assert lb.number_reading("1 210", "тыс/мкл", cfg["platelets"]) == ("ok", 1210.0)
    assert lb.number_reading("1 210", None, cfg["ferritin"]) == ("ok", 1210.0)       # неразрывный пробел
    assert lb.number_reading("12 500,5", None, cfg["vitamin_B12"]) == ("ok", 12500.5)
    assert lb.number_reading("141 102", "г/л", cfg["hemoglobin"]) == ("multi", None)
    assert lb.number_reading("45 108", None, cfg["ferritin"]) == ("ambiguous", None)
    assert lb.number_reading("1.210", None, cfg["ferritin"]) == ("ambiguous", None)
    assert lb.number_reading("1,210", "тыс/мкл", cfg["platelets"]) == ("ambiguous", None)
    # 1210 вне жёсткого диапазона — три знака после запятой обычные: эритроциты, ТТГ, креатинин в мг/дл.
    assert lb.number_reading("4,520", None, cfg["RBC"]) == ("ok", 4.52)
    assert lb.number_reading("1.234", None, cfg["TSH"]) == ("ok", 1.234)
    assert lb.number_reading("1,050", "мг/дл", cfg["creatinine"]) == ("ok", 1.05)
    assert lb.number_reading("0,405", "л/л", cfg["hematocrit"]) == ("ok", 0.405)       # ведущий ноль — десятичная


def test_thousands_in_text_lines():
    text = "\n".join([
        "Ферритин 1 210 мкг/л 10 - 120",
        "Тромбоциты 1 210 тыс/мкл 150-400",
        "Витамин B12 1 210 пг/мл 187 - 883",            # узкий неразрывный пробел
        "Лейкоциты 4 109/л 4-9",                              # «109/л» — степень десяти, не группа тысяч
        "Эритроциты 4,520 10^12/л 3,8-5,1",
        "Креатинин 75 150-400",                              # «75 150» — значение и норма, не 75150
        "Лактатдегидрогеназа 1.210 Ед/л 125 - 220",
        "Гемоглобин 141 102 г/л 117-160",
    ])
    r = _ext(text)
    v = _vals(r)
    assert v["ferritin"] == (1210.0, "мкг/л") and v["platelets"] == (1210.0, "тыс/мкл")
    assert v["vitamin_B12"] == (1210.0, "пг/мл")
    assert v["WBC"][0] == 4.0 and v["WBC"][1] in ("10^9/л", "10^9/l")
    assert v["RBC"][0] == 4.52 and v["creatinine"] == (75.0, None)
    assert r["reference_ranges"]["creatinine"] == {"low": 150.0, "high": 400.0, "unit": "мкмоль/л"}
    assert "LDH" not in v and "hemoglobin" not in v
    assert "строка 7: Лактатдегидрогеназа — проверьте значение" in r["unrecognized"]
    assert "строка 8: Гемоглобин — несколько результатов" in r["unrecognized"]


def test_two_results_in_one_line_are_not_taken():
    """H2 (в): «Гемоглобин 141 102 г/л», «45 8», «128 → 135» — предыдущий и текущий результаты: значение не берётся."""
    text = "\n".join([
        "Показатель Предыдущий (12.01.2025) Текущий Ед. Норма",
        "Гемоглобин 141 102 г/л 117-160",
        "Ферритин 45 8 мкг/л 10-120",
        "MCV 90 74 фл 80-100",
        "Гематокрит: 38 -> 35 %",
        "СОЭ 12 → 25 мм/ч",
    ])
    r = _ext(text)
    assert r["values"] == {}, r["values"]
    assert all(u.endswith("несколько результатов") for u in r["unrecognized"]) and len(r["unrecognized"]) == 5
    assert any(n.startswith("Гемоглобин: в строке несколько результатов") for n in r["notes"])
    # Норма, степень десяти, пометка и дата за значением — не второй результат.
    r = _ext("Эритроциты 4,05 3,9-5,2 10^12/л\nЛейкоциты 6,1 10*9/л\nС-реактивный белок 3,2 < 5 мг/л\n"
             "Гемоглобин 112 ↓ 117-160 г/л\nФерритин 9 до 10\nТромбоциты 251 x10^9/л")
    assert set(r["values"]) == {"RBC", "WBC", "CRP", "hemoglobin", "ferritin", "platelets"}, r


def test_range_instead_of_value_is_not_a_value():
    r = _ext("Гемоглобин 120-150\nЭритроциты 1-2")
    assert r["values"] == {} and r["unrecognized"] == ["строка 1: Гемоглобин — нет числа",
                                                       "строка 2: Эритроциты — нет числа"]


def test_urine_rows_are_not_blood_values():
    """H3: строки мочи (название, раздел, единица) не становятся показателями крови."""
    text = "\n".join([
        "Общий анализ мочи",
        "Эритроциты 1-2 в п/зр 0-2",
        "Лейкоциты 3 в п/зр 0 - 5",
        "Удельный вес 1015",
        "Общий анализ крови",
        "Эритроциты 4,2 10^12/л 3,8-5,1",
        "Лейкоциты 6,1 10^9/л 4-9",
    ])
    r = _ext(text)
    assert _vals(r) == {"RBC": (4.2, "10^12/л"), "WBC": (6.1, "10^9/л")}, r
    assert r["ignored"] == ["Эритроциты (моча)", "Лейкоциты (моча)", "Анализ мочи"]
    assert not any("несколько раз" in n for n in r["notes"]) and r["unrecognized"] == []
    # Слова в названии — без заголовка раздела; моча раньше крови.
    r = _ext("Эритроциты (в моче) 1 в п/зр 0-2\nГемоглобин (в моче) 120\nЛейкоциты, в п/зр 3\n"
             "Гемоглобин в моче 0,3 мг/л\nЭритроциты 4,2 10^12/л\nГемоглобин 128 г/л")
    assert _vals(r) == {"RBC": (4.2, "10^12/л"), "hemoglobin": (128.0, "г/л")}, r
    assert r["ignored"] == ["Эритроциты (моча)", "Гемоглобин (моча)", "Лейкоциты (моча)"]
    # Единица мочи без других признаков — значение отклоняется с замечанием.
    r = _ext("Эритроциты 12 кл/мкл 0-17\nЛейкоциты 3 в поле зрения")
    assert r["values"] == {} and r["unrecognized"] == ["строка 1: Эритроциты — единица анализа мочи",
                                                       "строка 2: Лейкоциты — единица анализа мочи"]
    assert any("это анализ мочи, а не крови" in n for n in r["notes"])
    # Биохимия крови со словами «моче-», «кал-» — не моча и не кал.
    assert lb.material_of("Мочевина") is None and lb.material_of("Мочевая кислота") is None
    assert lb.material_of("Калий") is None and lb.material_of("Эритроциты в кале") == "кал"
    assert lb.section_of("Общий анализ мочи от 05.02.2026") == "моча"
    assert lb.section_of("Клинический анализ крови") == "кровь" and lb.section_of("Гемоглобин 120") is None


def test_duplicates_prefer_value_with_recognized_unit():
    r = _ext("Эритроциты 4,5 ед\nЭритроциты 4,2 10^12/л 3,8-5,1")
    assert _vals(r) == {"RBC": (4.2, "10^12/л")}
    assert r["reference_ranges"]["RBC"] == {"low": 3.8, "high": 5.1, "unit": "×10¹²/л"}
    assert "Эритроциты встречается несколько раз: взято значение с распознанной единицей." in r["notes"]
    assert not any("единица не распознана" in n for n in r["notes"]), r["notes"]
    # Без единицы и с единицей — как раньше: первое значение (единица не указана — не «нераспознанная»).
    r = _ext("Hb - 131\nгемоглобин 99 г/л")
    assert r["values"]["hemoglobin"]["value"] == 131.0


def test_rdw_in_femtoliters_is_rdw_sd():
    """M2: «RDW 42,1 фл», «RDW (SD) 42 фл» — RDW-SD (ignored), RDW-CV в % не теряется как дубль."""
    r = _ext("RDW 42,1 фл 37 - 54\nRDW (SD) 42 фл\nRDW-CV 13,2 % 11,5 - 14,5")
    assert _vals(r) == {"RDW": (13.2, "%")} and r["ignored"] == ["RDW-SD"]
    assert r["reference_ranges"]["RDW"]["low"] == 11.5 and not any("несколько раз" in n for n in r["notes"])
    r = _ext("", tables=[[["Тест", "Результат", "Ед."], ["RDW", "42", "фл"], ["RDW", "13,1", "%"]]])
    assert _vals(r) == {"RDW": (13.1, "%")} and r["ignored"] == ["RDW-SD"]


def test_esr_westergren_preferred_panchenkov_noted():
    r = _ext("СОЭ (по Панченкову) 12 мм/ч 2 - 15\nСОЭ по Вестергрену 25 мм/ч 0 - 20")
    assert _vals(r) == {"ESR": (25.0, "мм/ч")}
    assert r["reference_ranges"]["ESR"] == {"low": 0.0, "high": 20.0, "unit": "мм/ч"}
    assert [n for n in r["notes"] if n.startswith("СОЭ")] == [
        "СОЭ: в бланке два метода — взято значение по Вестергрену (референтный метод)."]
    r = _ext("СОЭ по Вестергрену 25 мм/ч\nСОЭ (по Панченкову) 12 мм/ч")
    assert _vals(r) == {"ESR": (25.0, "мм/ч")} and not any("несколько раз" in n for n in r["notes"])
    r = _ext("Скорость оседания эритроцитов (по Панченкову) 14 мм/ч")
    assert _vals(r) == {"ESR": (14.0, "мм/ч")} and any(n.startswith("СОЭ: метод Панченкова") for n in r["notes"])
    assert not any("Панченков" in n for n in _ext("СОЭ 14 мм/ч")["notes"])


def test_birth_previous_old_and_future_dates_are_not_analysis_date():
    """H4 / M6: дата рождения в любом написании, дата предыдущего анализа, даты старше 15 лет и позже завтра."""
    import datetime as dt
    today = dt.date(2026, 10, 4)

    def d(*lines):
        return lb.find_date(list(lines), today)
    for birth in ("Дата р.: 12.03.2024", "Д/р: 12.03.2024", "д.р. 12.03.2024", "ДР: 12.03.2024",
                  "Дата р-я: 12.03.2024", "Дата рсждения: 12.03.2024", "Дата рождсния: 12.03.2024",
                  "Дата рожд.: 12.03.2024", "Дата: 12.03.2024 (дата рождения)", "Дата 12.03.2024 г.р.",
                  "Date of birth / дата: 12.03.2024", "Дата р. 12.03.2024"):
        assert d(birth) is None, birth                         # ребёнок: дата рождения моложе 5 лет
    assert d("Пациент: Проверочная, Дата р.: 14.06.2023", "Дата: 05.02.2026") == "2026-02-05"
    assert d("Дата: 12.03.2024 (дата рождения) Дата взятия: 05.02.2026") == "2026-02-05"
    assert d("Возраст 30 лет. Дата 12.03.1996") is None          # старше 15 лет: дата рождения взрослого без подписи
    assert d("Дата: 05.02.2011") is None and d("Дата: 05.11.2012") == "2012-11-05"   # граница — 15 лет
    assert d("Дата печати: 06.10.2026") is None and d("Дата: 05.10.2026") == "2026-10-05"   # не позже завтра
    assert d("Дата выполнения: 06.02.2026; дата предыдущего анализа: 12.01.2026") == "2026-02-06"
    assert d("Дата предыдущего анализа: 12.01.2026; дата выполнения: 06.02.2026") == "2026-02-06"
    assert d("Дата предыдущего результата: 12.01.2026") is None
    assert d("Поликлиника РЖД-Медицина. Дата: 05.02.2026") == "2026-02-05", "РЖД — не «рождения»"
    assert d("Дата регистрации: 03.03.2026") == "2026-03-03"
    # Ячейка таблицы «Дата р. | …» (дата рождения ребёнка) — не дата анализа.
    r = _ext("", tables=[[["Пациент", "Проверочная А.С."], ["Дата р.", "12.03.2024"]],
                         [["Тест", "Результат", "Ед."], ["Гемоглобин", "118", "г/л"]]])
    assert r["date"] is None and r["values"] == {"hemoglobin": {"value": 118.0, "unit": "г/л"}}


def test_tables_previous_column_dynamics_and_two_results_in_cell():
    """H2 (а, б): столбец прошлого результата левее «Результат»; таблица динамики с датами в заголовке."""
    prev_left = [["Тест", "Предыдущий результат", "Результат", "Ед. изм."],
                 ["Гемоглобин", "141", "102", "г/л"], ["Ферритин", "45", "8", "мкг/л"]]
    r = _ext("", tables=[prev_left])
    assert _vals(r) == {"hemoglobin": (102.0, "г/л"), "ferritin": (8.0, "мкг/л")}
    dyn = [["Тест", "12.01.2025", "05.02.2026", "Единицы", "Референсные значения"],
           ["Гемоглобин", "14,1", "10,2", "г/дл", "11,7 - 16,0"], ["Ферритин", "45", "8", "нг/мл", "10 - 120"]]
    r = _ext("", tables=[dyn])
    assert _vals(r) == {"hemoglobin": (10.2, "г/дл"), "ferritin": (8.0, "нг/мл")} and r["date"] == "2026-02-05"
    assert r["reference_ranges"]["hemoglobin"] == {"low": 117.0, "high": 160.0, "unit": "г/л"}
    # Порядок дат обратный — всё равно последняя дата.
    rev = [["Тест", "05.02.2026", "12.01.2025", "Ед."], ["Ферритин", "8", "45", "нг/мл"]]
    assert _vals(_ext("", tables=[rev])) == {"ferritin": (8.0, "нг/мл")}
    # Последнюю дату не определить (одинаковые даты) — значение не берётся.
    same = [["Тест", "05.02.2026", "05.02.2026", "Ед."], ["Ферритин", "8", "45", "нг/мл"]]
    r = _ext("", tables=[same])
    assert r["values"] == {} and r["unrecognized"] == ["строка таблицы 1: Ферритин — несколько результатов"]
    # Два результата в ячейке значения; число с разделителем тысяч в ячейке.
    cells = [["Тест", "Результат", "Ед."], ["Гемоглобин", "128 → 135", "г/л"], ["Тромбоциты", "1 210", "тыс/мкл"],
             ["Ферритин", "1.210", "мкг/л"]]
    r = _ext("", tables=[cells])
    assert _vals(r) == {"platelets": (1210.0, "тыс/мкл")}, r
    assert r["unrecognized"] == ["строка таблицы 1: Гемоглобин — несколько результатов",
                                 "строка таблицы 3: Ферритин — проверьте значение"]


def test_tables_urine_section_from_text_above_and_row_heading():
    """H3: раздел мочи по тексту над таблицей (above) и по строке-заголовку внутри таблицы."""
    urine = [["Исследование", "Результат", "Единицы"], ["Эритроциты", "1-2", "в п/зр"], ["Лейкоциты", "3", ""],
             ["Гемоглобин", "отрицательно", ""]]
    blood = [["Исследование", "Результат", "Единицы"], ["Эритроциты", "3,9", "10^12/л"], ["Лейкоциты", "5,2", "10^9/л"]]
    r = _ext("", tables=[urine, blood], table_above=["Дата взятия: 05.02.2026\nОбщий анализ мочи", "Общий анализ крови"])
    assert _vals(r) == {"RBC": (3.9, "10^12/л"), "WBC": (5.2, "10^9/л")}, r
    assert r["ignored"] == ["Эритроциты (моча)", "Лейкоциты (моча)", "Гемоглобин (моча)"]
    one = [["Исследование", "Результат", "Единицы"], ["Общий анализ мочи", "", ""], ["Лейкоциты", "3", ""],
           ["Клинический анализ крови", "", ""], ["Лейкоциты", "5,2", "10^9/л"]]
    r = _ext("", tables=[one])
    assert _vals(r) == {"WBC": (5.2, "10^9/л")} and r["ignored"] == ["Лейкоциты (моча)"], r
