"""Общие помощники разбора бланка анализов любой лаборатории (ЕМИАС, Инвитро, Гемотест, KDL, Хеликс, CMD, поликлиника):
названия показателей, единицы, нормы с бланка, дата анализа и сбор результата. Используют text_parser (строки текста,
фото, скан) и lab_tables (таблицы PDF).

Названия показателей (match_name). Источник синонимов — config/analytes.yaml: коды, aliases, short_ru, name_ru
(используемые показатели) и раздел not_used (известные, но не используемые: лейкоформула, MPV, PDW, PCT, RDW-SD…).
  * нормализация: нижний регистр, «ё» = «е», тире и дефисы — один знак, запятые и лишние пробелы убираются,
    одинаковые на вид кириллица и латиница приводятся к латинице («Витамин В12» с русской «В», «МСН»);
  * хвосты в скобках («(по Вестергрену)», «(HGB)», «(тромбокрит, PCT)») отделяются от названия;
  * ищется ТОЛЬКО полное совпадение названия с синонимом; если его нет — совпадение аббревиатуры в скобках
    (или отдельного слова заглавными латинскими буквами: «Гемоглобин HGB»). Если аббревиатура указывает на другой
    показатель, чем название, верна аббревиатура: «Ширина распределения эритроцитов (RDW-SD)» — это RDW-SD, а не RDW.
  * поиска подстроки нет: «Средний объём тромбоцитов» не станет MCV, «Абсолютное количество нейтрофилов» —
    лейкоцитами, «Тромбокрит» — гематокритом, «Гликированный гемоглобин» — гемоглобином.

Единицы (unit_norm, unit_for_cell, unit_in_rest). Единица с бланка сравнивается с перечнем units показателя
в analytes.yaml (тем же, по которому normalize.py пересчитывает значение множителем при расчёте). Формы степени
десяти приводятся к одной: «×10⁹/л», «x10^9/l», «10*9/л», «10e9/л», «109/л» -> «10^9/л». Значение здесь НЕ
пересчитывается: в ответ идёт написание единицы из analytes.yaml, пересчёт делает normalize.py. Неизвестная
единица — замечание «проверьте», значение не пересчитывается.

Нормы с бланка (find_range): «120-150», «120 – 150», «3,9…5,2», «от 120 до 150», «< 5», «до 5», «менее 5»,
«> 30», «более 30». В ответ (reference_ranges) — в канонической единице показателя: граница × тот же множитель
analytes.yaml, что у значения (г/дл × 10 = г/л; мкг/дл × 0,179 = мкмоль/л); «< 5» -> low = null.

Дата анализа (find_date): «Дата взятия (биоматериала)», «Дата выполнения», «Дата регистрации / выдачи», «Дата: …».
Предпочтение: взятие > выполнение > прочие. Не берутся (ревизия разбора, H4 / M6):
  * дата рождения в любом написании: «рождения», «Дата р.», «Д/р», «д.р.», «ДР», «р-я», «г.р.», «Дата: … (дата
    рождения)» и искажения распознавания («рсждения», «poждения» с латинскими буквами);
  * дата «предыдущего» анализа («дата предыдущего результата», «прошлого», «ранее»);
  * даты старше 15 лет от сегодняшнего дня (MAX_DATE_AGE_DAYS) и позже завтрашнего дня. Сервис — для взрослых
    (≥ 18 лет), поэтому дата рождения пациента всегда старше 15 лет и сюда не пройдёт даже без подписи; при этом
    старые анализы (до 15 лет) для истории в кабинете дату с бланка сохраняют (координатор, 04.10 вечер).

Число значения (number_reading; ревизия разбора, H1). Пробел внутри числа — разделитель тысяч: «1 210» = 1210
(тромбоциты «1 210 тыс/мкл», ферритин «1 210 мкг/л»). Если так прочитанное число вне жёсткого диапазона показателя,
а первая группа в нём — это «несколько результатов подряд» («Гемоглобин 141 102 г/л»: предыдущий и текущий):
значение не берётся. Если число с тысячами вне мягкого диапазона, а каждая группа в него попадает («Ферритин 45 108») —
неясно, одно это число или два: значение не берётся, замечание. «1.210» / «1,210» (ровно три цифры после
разделителя) у показателя, где 1210 — допустимое значение (тромбоциты, ферритин, B12, ЛДГ, креатинин…), неоднозначно
(десятичная точка или разделитель тысяч): значение не берётся, замечание «проверьте». Значение молча не искажается.

Другой биоматериал (ревизия разбора, H3). Строки общего анализа мочи и кала не сопоставляются с показателями
крови: название со словами «моча / в моче / urine / осадок / в п/зр / в поле зрения / кал» (material_of), строка
под заголовком раздела «Общий анализ мочи / ОАМ / Моча / Осадок / Копрограмма» до заголовка раздела крови
(section_of) — в ignored как «<показатель> (моча)»; значение в единицах «в п/зр», «в поле зрения», «кл/мкл»,
«клеток/мкл» (foreign_unit) отклоняется с замечанием. Из нескольких значений одного показателя берётся значение
с распознанной единицей (Collector.add, ранг значения).

RDW в фл или «RDW (SD)» — это RDW-SD (ширина распределения в фемтолитрах, норма ~39–46 фл), а не RDW-CV в %:
в ignored (reroute; ревизия разбора, M2). СОЭ: если в бланке есть и метод Панченкова, и метод Вестергрена, берётся
Вестергрен — референтный метод ICSH (Kratz et al., Int J Lab Hematol 2017;39:448–457), на нём основаны пороги
норм; если только Панченков — значение берётся с замечанием (при высоких значениях методы расходятся).

Персональные данные: в ответ попадают только коды и названия из справочника, числа значений и норм, дата анализа.
Текст строк бланка (ФИО, полис, номер заказа, исполнитель, учреждение) не возвращается и не пишется в журнал.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from ..config import Config, config_dir
from ..normalize import unit_key

MAX_UNRECOGNIZED = 30
UNRECOGNIZED_WIDTH = 60

# --------------------------------------------------------------------------------------------
# Названия показателей
# --------------------------------------------------------------------------------------------
# Кириллица и латиница, одинаковые на вид: приводятся к латинице и в строке бланка, и в справочнике.
_HOMOGLYPHS = str.maketrans("авекмнорстух", "abekmhopctyx")
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})
_EDGE_PUNCT = " .:;|*!•·…=_/\\-"
_PARENS = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_OPEN_TAIL = re.compile(r"[(\[][^)\]]*$")
# Аббревиатура без скобок: слово заглавными латинскими буквами в начале или в конце названия («Гемоглобин HGB»).
_CAPS_TOKEN = re.compile(r"^[A-Z][A-Z0-9#%+\-]{1,7}$")

# Пометки отклонения: стрелки, звёздочки, «!», отдельные H / L, слова ЕМИАС «отклонение от нормы» и т. п.
_FLAG_PHRASES = re.compile(
    r"отклонени[ея]\s+от(?:\s+нормы)?|(?:ниже|выше)\s+нормы|в\s+пределах\s+нормы|\(?\s*числовой\s+результат\s*\)?"
    r"|критическ\w*\s+значени\w*|нормы\b", re.IGNORECASE)
_FLAG_TOKENS = re.compile(r"(?<!\S)(?:[↑↓▲▼⇑⇓↗↘]+|\*{1,3}|!{1,3}|HH?|LL?|\+{1,2})(?!\S)")
_ARROWS = re.compile(r"[↑↓▲▼⇑⇓↗↘]")


class Target(NamedTuple):
    """Результат сопоставления названия: используемый показатель (kind="analyte", code — код из analytes.yaml)
    или известный, но не используемый (kind="ignored", code — название для ответа из раздела not_used)."""
    kind: str
    code: str


def strip_flags(text: str) -> str:
    """Убирает пометки отклонения (↓ ↑ * ! H L, «Ниже нормы», «отклонение от нормы», «(числовой результат)»)."""
    text = _FLAG_PHRASES.sub(" ", str(text))
    text = _ARROWS.sub(" ", text)
    return _FLAG_TOKENS.sub(" ", text)


def name_key(s: Any) -> str:
    """Название для сравнения: нижний регистр, «ё» = «е», одно тире, без запятых и лишних пробелов и знаков по краям,
    кириллица одинакового вида -> латиница. Длина не важна: ключ используется только для точного совпадения."""
    s = str(s).casefold().replace("ё", "е").translate(_DASHES)
    s = re.sub(r"\s*-\s*", "-", s)              # «С - реактивный» = «С-реактивный»
    s = " ".join(s.replace(",", " ").split())
    return s.strip(_EDGE_PUNCT).translate(_HOMOGLYPHS)


def split_parens(text: str) -> tuple[str, list[str]]:
    """«Гемоглобин (HGB)» -> ("Гемоглобин", ["HGB"]); незакрытая скобка в конце («… (по» при переносе) отбрасывается."""
    items: list[str] = []

    def take(m: re.Match[str]) -> str:
        items.extend(p.strip() for p in re.split(r"[,;/]", m.group(1)) if p.strip())
        return " "

    base = str(text)
    for _ in range(3):                           # вложенные скобки — до трёх уровней
        new = _PARENS.sub(take, base)
        if new == base:
            break
        base = new
    base = _OPEN_TAIL.sub(" ", base)
    return " ".join(base.split()), items


def clean_name(raw: str) -> str:
    """Название из ячейки или начала строки: без пометок отклонения, маркеров списка и знаков по краям."""
    s = strip_flags(raw)
    s = re.sub(r"^\s*(?:\d{1,2}[.)]\s+|[-–—•·]\s*)", "", s)
    return " ".join(s.split()).strip(" .:;|*!•·…=_-–—")


@lru_cache(maxsize=4)
def _not_used_cached(path: str, mtime: float) -> tuple[tuple[str, tuple[str, ...]], ...]:
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = []
    for item in data.get("not_used") or []:
        if isinstance(item, dict) and item.get("name_ru"):
            out.append((str(item["name_ru"]), tuple(str(a) for a in item.get("aliases") or [])))
    return tuple(out)


def not_used_entries() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Раздел not_used из config/analytes.yaml: ((название для ответа, (синонимы…)), …)."""
    p = Path(config_dir()) / "analytes.yaml"
    try:
        return _not_used_cached(str(p), p.stat().st_mtime)
    except OSError:
        return ()


def _keys_for(name: str) -> list[str]:
    base, _ = split_parens(name)
    k = name_key(base)
    return [k] if k else []


_INDEX_CACHE: dict[int, tuple[Any, tuple, dict[str, Target]]] = {}


def synonym_table(cfg: Config) -> list[tuple[str, Target, str]]:
    """Все синонимы справочника: [(ключ, цель, исходное написание)] — для индекса и теста уникальности."""
    rows: list[tuple[str, Target, str]] = []
    for code in cfg.analytes:                                  # канонические коды важнее алиасов
        for k in {name_key(code), name_key(code.replace("_", " "))}:
            rows.append((k, Target("analyte", code), code))
    for code, spec in cfg.analytes.items():
        for alias in [*(spec.get("aliases") or []), spec.get("short_ru"), spec.get("name_ru")]:
            if alias:
                for k in _keys_for(alias):
                    rows.append((k, Target("analyte", code), str(alias)))
    for display, aliases in not_used_entries():
        for alias in (display, *aliases):
            for k in _keys_for(alias):
                rows.append((k, Target("ignored", display), alias))
    return rows


def name_index(cfg: Config) -> dict[str, Target]:
    """Ключ названия -> показатель. Используемые показатели важнее раздела not_used (пересечений и так нет)."""
    nu = not_used_entries()
    cached = _INDEX_CACHE.get(id(cfg.analytes))
    if cached is not None and cached[0] is cfg.analytes and cached[1] == nu:
        return cached[2]
    index: dict[str, Target] = {}
    for k, target, _ in synonym_table(cfg):
        index.setdefault(k, target)
    _INDEX_CACHE[id(cfg.analytes)] = (cfg.analytes, nu, index)
    return index


# Единица, приписанная к названию через запятую: «Гемоглобин, г/л», «Эозинофилы, %».
_NAME_UNIT = re.compile(r"^(?P<name>.+?),\s*(?P<unit>[^,]{1,16})$")
_UNIT_LIKE = re.compile(r"/|%|‰|^(?:фл|пг|fl|pg)$", re.IGNORECASE)


def split_name_unit(name: str) -> tuple[str, str | None]:
    """«Гемоглобин, г/л» -> («Гемоглобин», «г/л»); без единицы в конце — (название, None)."""
    m = _NAME_UNIT.match(str(name).strip())
    if m and _UNIT_LIKE.search(m.group("unit").strip()):
        return m.group("name"), m.group("unit").strip()
    return str(name), None


def match_name(raw: str, index: dict[str, Target]) -> Target | None:
    """Название с бланка -> показатель. Только полное совпадение (после нормализации) или аббревиатура в скобках;
    единица через запятую в конце названия («Эозинофилы, %») не мешает."""
    found = _match_name(raw, index)
    if found is None:
        base, unit = split_name_unit(clean_name(raw))
        if unit is not None:
            found = _match_name(base, index)
    return found


def _match_name(raw: str, index: dict[str, Target]) -> Target | None:
    text = clean_name(raw)
    if not text:
        return None
    base, items = split_parens(text)
    full = index.get(name_key(base)) if base else None
    candidates = list(items)
    tokens = base.split()
    if len(tokens) >= 2:
        for tok in (tokens[-1], tokens[0]):
            if _CAPS_TOKEN.match(tok):
                candidates.append(tok)
    abbr: list[Target] = []
    for a in candidates:
        t = index.get(name_key(a))
        if t is not None and t not in abbr:
            abbr.append(t)
    if full is not None:
        # Аббревиатура точнее названия: «Ширина распределения эритроцитов (RDW-SD)», «Гемоглобин (HbA1c)».
        if len(abbr) == 1 and abbr[0] != full:
            return abbr[0]
        return full
    return abbr[0] if len(abbr) == 1 else None


# --------------------------------------------------------------------------------------------
# Другой биоматериал (моча, кал) и уточнение показателя (RDW-SD, метод СОЭ)
# --------------------------------------------------------------------------------------------
# Слова в НАЗВАНИИ строки: «Эритроциты (в моче)», «Гемоглобин в моче», «Лейкоциты, в п/зр», «Эритроциты в кале».
# «Мочевина» и «Мочевая кислота» (биохимия крови) сюда не попадают: нужна форма слова «моча», а не «моче-».
_MATERIAL_IN_NAME: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("моча", re.compile(r"\bмоч[аеиу]\b|\bмочой\b|\burine\b|\burinalysis\b|\bосад(?:ок|ка|ке)\b|\bп\s*/\s*зр?\b"
                        r"|\bп\.\s*зр?\b|\bпол[еяю]\s+зрения\b", re.IGNORECASE)),
    ("кал", re.compile(r"\bкал[еау]?\b|копрограм", re.IGNORECASE)),
)
# Заголовок раздела (строка без чисел, кроме даты): начинает раздел другого биоматериала или раздел крови.
_SECTION_OTHER: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("моча", re.compile(
        r"^(?:(?:общий|клинический)\s+)?(?:анализ|исследование)\s+мочи\b|^моча\b|^оам\b"
        r"|^(?:микроскопи\w*\s+)?(?:мочевого\s+)?осад(?:ок|ка)\b|^мочевой\s+осадок|^(?:био)?материал\s*:?\s*моча\b"
        r"|^urin|^(?:анализ\s+мочи\s+)?по\s+(?:нечипоренко|зимницкому)\b", re.IGNORECASE)),
    ("кал", re.compile(r"^копрограмм|^(?:общий\s+)?анализ\s+кала\b|^кал\b|^(?:био)?материал\s*:?\s*кал\b",
                       re.IGNORECASE)),
)
_SECTION_BLOOD = re.compile(
    r"^(?:(?:общий|клинический|развернутый|биохимический|гематологический)\s+)*(?:анализ|исследование)\s+крови\b"
    r"|^оак\b|^кла\b|^гематолог|^гемограмм|^биохими|^гормон|^коагулограм|^гемостаз|^иммунолог|^серолог"
    r"|^витамины\b|^обмен\s+железа|^показатели\s+обмена|^(?:био)?материал\s*:?\s*(?:\w+\s+)?кровь\b|^кровь\s*$"
    r"|^сыворотка\b|^плазма\b|^cbc\b|^complete\s+blood|^hematolog|^biochem|^chemistry|^лейкоцитарн\w*\s+формул",
    re.IGNORECASE)
_DATE_ANY = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}")
MATERIAL_SECTION_NAME = {"моча": "Анализ мочи", "кал": "Анализ кала"}

# Единица счёта клеток в поле зрения микроскопа или клеток в микролитре мочи — другая размерность, не кровь:
# «в п/зр», «п/з», «в поле зрения», «кл/мкл», «клеток/мкл», «/hpf». «тыс/мкл», «млн/мкл» (кровь) сюда не попадают.
_FOREIGN_UNIT = re.compile(
    r"^\s*(?:в\s+)?(?:п\s*/\s*зр?\b|п\.\s*зр?\b|пол[еяю]\s+зрени|(?:кл|клет\w*|cells?)\.?\s*/\s*(?:мкл|ul|µl|μl|мл)\b"
    r"|/\s*(?:hpf|lpf)\b)", re.IGNORECASE)


def material_of(name: str) -> str | None:
    """Биоматериал по словам в названии строки: "моча", "кал" или None (кровь или не указано)."""
    s = str(name or "").casefold().replace("ё", "е")
    for material, rx in _MATERIAL_IN_NAME:
        if rx.search(s):
            return material
    return None


def section_of(line: str) -> str | None:
    """Строка-заголовок раздела бланка -> "моча" / "кал" (раздел другого биоматериала), "кровь" (раздел крови)
    или None (не заголовок). Заголовок — строка без чисел (дата допускается: «Общий анализ мочи от 05.02.2026»)."""
    s = clean_name(str(line or "")).casefold().replace("ё", "е")
    if not s or len(s) > 80 or re.search(r"\d", _DATE_ANY.sub(" ", s)):
        return None
    for material, rx in _SECTION_OTHER:
        if rx.search(s):
            return material
    return "кровь" if _SECTION_BLOOD.search(s) else None


def foreign_unit(text: str) -> bool:
    """Остаток строки после значения (или ячейка единицы) начинается с единицы анализа мочи («в п/зр», «кл/мкл»)."""
    return bool(_FOREIGN_UNIT.match(strip_flags(str(text or ""))))


_MATERIAL_WORDS = re.compile(
    r"\b(?:в|из)\s+(?:моче|кале)\b|\b(?:моча|мочи|urine)\b|,?\s*\bв\s+(?:п\s*/\s*зр?|п\.\s*зр?|поле\s+зрения)\b.*$",
    re.IGNORECASE)


def material_target(name: str, index: dict[str, Target]) -> Target | None:
    """Показатель в строке мочи или кала без слов о биоматериале: «Гемоглобин в моче» -> Гемоглобин (только для
    названия в ignored: «Гемоглобин (моча)»; значение такой строки не берётся)."""
    base, _ = split_parens(clean_name(name))
    base = _MATERIAL_WORDS.sub(" ", base)
    return match_name(base, index) if base.strip() else None


def material_display(target: Target | None, cfg: Config, material: str) -> str:
    """Название для ignored: «Эритроциты (моча)»; строка раздела мочи без известного названия — «Анализ мочи»."""
    if target is None:
        return MATERIAL_SECTION_NAME.get(material, material)
    name = cfg.analytes[target.code].get("name_ru", target.code) if target.kind == "analyte" else target.code
    return f"{name} ({material})"


RDW_SD_DISPLAY = "RDW-SD"
_FL_UNIT = re.compile(r"\s*(?:фл|fl)(?![a-zа-яё])", re.IGNORECASE)        # единица в начале остатка или ячейки


def reroute(target: Target, name: str, unit_text: str | None) -> Target:
    """Уточнение показателя по единице и хвосту названия. RDW в фемтолитрах («RDW 42,1 фл») или «RDW (SD)» — это
    RDW-SD (стандартное отклонение объёма эритроцитов, фл), а не RDW-CV (коэффициент вариации, %), который нужен
    правилам: такая строка — в ignored, иначе 42 «%» принимается за резко повышенный RDW."""
    if target.kind != "analyte" or target.code != "RDW":
        return target
    _, items = split_parens(clean_name(name))
    words = name_key(name).replace("-", " ").split()
    if any(name_key(i) == "sd" for i in items) or "sd" in words or _FL_UNIT.match(strip_flags(str(unit_text or ""))):
        return Target("ignored", RDW_SD_DISPLAY)
    return target


def esr_method(name: str) -> str | None:
    """Метод СОЭ по названию строки: "westergren", "panchenkov" или None (не указан)."""
    s = str(name or "").casefold().replace("ё", "е")
    if "вестергрен" in s or "westergren" in s:
        return "westergren"
    if "панченков" in s or "panchenkov" in s:
        return "panchenkov"
    return None


def prefix_analyte(raw: str, index: dict[str, Target]) -> str | None:
    """Код показателя, с которого НАЧИНАЕТСЯ строка без числа («Ферритин FFs мкг/л»). Только для замечания
    «нет числа» (значение из такой строки не берётся); строка, целиком совпавшая с not_used, не считается."""
    text = clean_name(raw)
    full = match_name(text, index)
    if full is not None:
        return full.code if full.kind == "analyte" else None
    words = text.split()
    for k in range(min(len(words), 8), 0, -1):
        t = index.get(name_key(" ".join(words[:k])))
        if t is not None:
            return t.code if t.kind == "analyte" else None
    return None


# --------------------------------------------------------------------------------------------
# Единицы
# --------------------------------------------------------------------------------------------
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
# Степень десяти в строке бланка: «×10⁹/л», «x 10^9/l», «10*9/л», «10e9/л», «109/л» (знак степени потерян в PDF).
POW_UNIT = re.compile(r"(?<![\d.,])(?:[x×х*·]\s*)?10\s*(?:\^|\*|e|е)?\s*([0-9⁰¹²³⁴⁵⁶⁷⁸⁹]{1,2})\s*/\s*([a-zа-яµμ]+)",
                      re.IGNORECASE)


def unit_norm(text: Any) -> str:
    """Единица для сравнения: нижний регистр, без пробелов, «μ» = «µ», надстрочные цифры — обычные; все формы
    степени десяти -> «10^N/…»."""
    s = unit_key(str(text).translate(SUPERSCRIPTS))
    s = re.sub(r"^[x×х*·]+", "", s)
    m = re.match(r"^10(?:\^|\*|e|е)?(\d{1,2})/(.+)$", s)
    if m:
        s = f"10^{m.group(1)}/{m.group(2)}"
    return s


def unit_table(spec: dict) -> dict[str, str]:
    """Нормализованная единица -> написание из analytes.yaml (его понимает normalize.py при пересчёте)."""
    table: dict[str, str] = {}
    for u in spec.get("units") or {}:
        table.setdefault(unit_norm(u), str(u))
    for canonical in (spec.get("unit"), spec.get("unit_ru")):
        if canonical:
            table.setdefault(unit_norm(canonical), str(canonical))
    return table


def unit_factor(spec: dict, unit: str | None) -> float:
    """Множитель к канонической единице для написания из analytes.yaml (None — каноническая)."""
    if unit is None:
        return 1.0
    units = spec.get("units") or {}
    if unit in units:
        return float(units[unit])
    return 1.0


def unit_for_cell(cell: str, spec: dict) -> tuple[str | None, bool]:
    """Единица из отдельного столбца -> (написание из analytes.yaml | None, указана ли неизвестная единица)."""
    s = " ".join(str(cell or "").split())
    if not s or s in ("-", "—", "–"):
        return None, False
    found = unit_table(spec).get(unit_norm(s))
    return (found, False) if found else (None, True)


_NORM_WORDS = re.compile(r"\b(?:норм[аы]?|референс\w*|реф\.?|ref\w*|интервал|диапазон)\b", re.IGNORECASE)
_DASH_TOKENS = re.compile(r"(?<!\S)[-–—]+(?!\S)")


def unit_in_rest(rest: str, spec: dict) -> tuple[str | None, bool]:
    """Единица в остатке строки после значения (норма и пометки уже вырезаны): степень десяти где угодно,
    иначе — в начале остатка. -> (написание из analytes.yaml | None, указана ли неизвестная единица)."""
    table = unit_table(spec)
    m = POW_UNIT.search(rest)
    if m:
        found = table.get(unit_norm(m.group(0)))
        return (found, False) if found else (None, True)
    cleaned = _NORM_WORDS.sub(" ", rest)
    cleaned = re.sub(r"[()\[\]:;|]", " ", cleaned)
    cleaned = _DASH_TOKENS.sub(" ", cleaned).strip()
    if not cleaned:
        return None, False
    compact = unit_key(cleaned.translate(SUPERSCRIPTS))
    for key in sorted(table, key=len, reverse=True):
        if compact.startswith(key):
            return table[key], False
    # Что-то похожее на единицу есть, но в перечне её нет: замечание без текста (за числом бывает фамилия).
    return None, bool(re.match(r"[^\s\d(\[]{1,12}(?:/[^\s(\[]{1,8})?", cleaned))


# --------------------------------------------------------------------------------------------
# Нормы с бланка
# --------------------------------------------------------------------------------------------
_NUM = r"\d+(?:[.,]\d+)?"
# Диапазон «a-b»: число не продолжает другое число или дефисную цепочку («112-233-445» — не норма, это номер).
RANGE = re.compile(
    rf"(?<![\w.,^/*\-–—])(?P<a>{_NUM})\s*(?:-|–|—|−|‑|\.\.\.?|…)\s*(?P<b>{_NUM})(?![\d.,]|\s*[-–—]\s*\d)"
    rf"|(?<![\w])от\s*(?P<c>{_NUM})\s*до\s*(?P<d>{_NUM})(?![\d.,])"
    rf"|(?:<=|≤|<|(?<![\w])(?:до|менее|меньше|ниже|не\s+более))\s*(?P<hi>{_NUM})(?![\d.,])"
    rf"|(?:>=|≥|>|(?<![\w])(?:от|более|больше|выше|свыше|не\s+менее))\s*(?P<lo>{_NUM})(?![\d.,])",
    re.IGNORECASE)


def _num(s: str | None) -> float | None:
    return None if s is None else float(s.replace(",", "."))


def range_from_match(m: re.Match[str]) -> tuple[float | None, float | None]:
    if m.group("a") is not None:
        return _num(m.group("a")), _num(m.group("b"))
    if m.group("c") is not None:
        return _num(m.group("c")), _num(m.group("d"))
    if m.group("hi") is not None:
        return None, _num(m.group("hi"))
    return _num(m.group("lo")), None


def find_range(text: str) -> tuple[tuple[float | None, float | None], tuple[int, int]] | None:
    """Первая норма в тексте -> ((нижняя | None, верхняя | None), (начало, конец))."""
    m = RANGE.search(str(text or ""))
    if m is None:
        return None
    return range_from_match(m), m.span()


# --------------------------------------------------------------------------------------------
# Дата анализа
# --------------------------------------------------------------------------------------------
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря")
_DATE_RX = re.compile(
    r"(?<!\d)(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>\d{4}|\d{2})(?!\d)"
    r"|(?<!\d)(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})(?!\d)"
    r"|(?<!\d)(?P<d3>\d{1,2})\s+(?P<mon>" + "|".join(_MONTHS) + r")\s+(?P<y3>\d{4})(?!\d)", re.IGNORECASE)
def _words(*words: str) -> re.Pattern[str]:
    """Подписи сравниваются по «скелету» (кириллица и латиница одинакового вида — одно и то же): распознанное
    фото или скан часто даёт «poждения» с латинскими «p», «o» — дату рождения нельзя принять за дату анализа."""
    return re.compile("|".join(re.escape(w.translate(_HOMOGLYPHS)) for w in words))


def _skeleton_rx(pattern: str) -> re.Pattern[str]:
    """Регулярное выражение по «скелету»: кириллица одинакового с латиницей вида в шаблоне заменяется так же, как
    в тексте подписи (служебные \\b, \\s, \\S, \\d — латиница, они не меняются)."""
    return re.compile(pattern.translate(_HOMOGLYPHS))


MAX_DATE_AGE_DAYS = 15 * 365 + 4        # старше 15 лет — не дата анализа (дата рождения взрослого ≥ 18 лет)
# Дата рождения: «рождения», «рожд.», искажения распознавания («рсждения», «ро>кдения»), «Дата р.», «Д/р», «д.р.»,
# «ДР», «р-я», «г.р.», «birth», «DOB». «Дата регистрации», «РЖД» (название клиники) — не рождение.
_P_BIRTH = _skeleton_rx(r"рожд|\bр\S{1,2}жд|\bр\S{1,3}дени|г\.\s*р\b|\bд\s*[./]?\s*р\b|дата\s*р\b|\bр\s*-\s*я\b"
                        r"|birth|\bdob\b")
# Подпись сразу ПОСЛЕ даты: «12.03.1996 (дата рождения)», «12.03.1996 г.р.».
_BIRTH_AFTER = _skeleton_rx(r"^\s*(?:\(([^()]{0,40})\)|г\.\s*р\b)")
# Дата предыдущего анализа (столбец «Дата предыдущего результата», «прошлого исследования»).
_P_PREV = _skeleton_rx(r"предыдущ|прошл|\bранее\b|\bпред\.")
_P_TAKEN = _words("взят", "забор", "сбор")
_P_OTHER = _words("регистр", "выдач", "печат", "поступ", "заказ", "оформ", "прием")
_P_DONE = _words("выполн", "исследован", "готов", "результат", "анализ", "валид", "одобр", "утвержд")
_P_DATE = _words("дата")
_P_ANY = _words("дат", "взят", "выполн", "забор")


def _label_priority(label: str) -> int | None:
    """Приоритет даты по подписи перед ней: 1 — взятие, 2 — выполнение, 3 — прочие даты бланка; None — не дата
    анализа (дата рождения или подписи нет вовсе)."""
    sk = label.translate(_HOMOGLYPHS)
    if _P_BIRTH.search(sk) or _P_PREV.search(sk):
        return None
    if _P_TAKEN.search(sk):
        return 1
    if _P_OTHER.search(sk):
        return 3
    if _P_DONE.search(sk):
        return 2
    if _P_DATE.search(sk):
        return 3
    return None


def _date_of(m: re.Match[str]) -> dt.date | None:
    try:
        if m.group("d") is not None:
            y = int(m.group("y"))
            y = y + 2000 if y < 100 else y
            return dt.date(y, int(m.group("m")), int(m.group("d")))
        if m.group("y2") is not None:
            return dt.date(int(m.group("y2")), int(m.group("m2")), int(m.group("d2")))
        return dt.date(int(m.group("y3")), _MONTHS.index(m.group("mon").lower()) + 1, int(m.group("d3")))
    except ValueError:
        return None


def _birth_after(after: str) -> bool:
    """Сразу после даты — пометка, что это дата рождения: «(дата рождения)», «(д.р.)», «г.р.»."""
    m = _BIRTH_AFTER.match(after.translate(_HOMOGLYPHS))
    return m is not None and (m.group(1) is None or bool(_P_BIRTH.search(m.group(1))))


_DATE_AT_START = re.compile(r"^\s*(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[а-яё]+\s+\d{4})(?!\d)",
                            re.IGNORECASE)


def _join_label_lines(lines: list[str]) -> list[str]:
    """Подпись и дата на соседних строках («Дата взятия:» / «01.09.2026 08:40» — так копируется строка таблицы из
    двух ячеек): строка с подписью без даты склеивается со следующей непустой строкой, если та начинается с даты."""
    out = list(lines)
    for i, line in enumerate(lines):
        low = line.casefold().replace("ё", "е")
        if len(low) > 60 or not _P_ANY.search(low.translate(_HOMOGLYPHS)) or _DATE_RX.search(low):
            continue
        nxt = next((x for x in lines[i + 1:i + 3] if x.strip()), "")
        if _DATE_AT_START.match(nxt):
            out[i] = line + " " + nxt
    return out


def find_date(lines: Iterable[str], today: dt.date | None = None) -> str | None:
    """Дата анализа с бланка (ISO) или None. Подпись ищется в той же строке между предыдущей датой и этой (или в
    строке выше, если в ней только подпись); пометка «(дата рождения)» — и сразу после даты. Берутся только даты не
    старше MAX_DATE_AGE_DAYS и не позже завтра."""
    today = today or dt.date.today()
    oldest, latest = today - dt.timedelta(days=MAX_DATE_AGE_DAYS), today + dt.timedelta(days=1)
    best: tuple[int, dt.date] | None = None
    for line in _join_label_lines([str(x) for x in lines]):
        low = str(line).casefold().replace("ё", "е")
        if not _P_ANY.search(low.translate(_HOMOGLYPHS)):
            continue
        prev = 0
        matches = list(_DATE_RX.finditer(low))
        for k, m in enumerate(matches):
            label = low[prev:m.start()]
            if prev:                                 # «(дата рождения)», «г.р.» сразу за прошлой датой — её пометка
                tail = _BIRTH_AFTER.match(label.translate(_HOMOGLYPHS))
                label = label[tail.end():] if tail else label
            prev = m.end()
            prio = _label_priority(label)
            if prio is None:
                continue
            if _birth_after(low[m.end():matches[k + 1].start() if k + 1 < len(matches) else len(low)]):
                continue
            d = _date_of(m)
            if d is None or not (oldest <= d <= latest):
                continue
            if best is None or prio < best[0]:
                best = (prio, d)
    return best[1].isoformat() if best else None


# --------------------------------------------------------------------------------------------
# Сбор результата разбора
# --------------------------------------------------------------------------------------------
def within_hard(value: float, unit: str | None, spec: dict) -> bool:
    """Попадает ли значение (в указанной единице) в жёсткий диапазон показателя из analytes.yaml. Единица не
    распознана — при расчёте будет принята каноническая, поэтому и проверяем как каноническую."""
    hard = spec.get("hard")
    if not hard:
        return True
    return hard[0] <= value * unit_factor(spec, unit) <= hard[1]


def _within(bounds, value: float, unit: str | None, spec: dict) -> bool:
    return not bounds or bounds[0] <= value * unit_factor(spec, unit) <= bounds[1]


# Разделитель групп тысяч: пробел, неразрывный, узкий неразрывный и тонкий пробел.
THOUSANDS_SEP = "[    ]"
_THOUSANDS_SPLIT = re.compile(THOUSANDS_SEP)
# Ровно три цифры после точки или запятой, целая часть без ведущего нуля: «1.210», «2,450» (но не «0,405»).
_THREE_DECIMALS = re.compile(r"^[1-9]\d{0,2}[.,]\d{3}$")


def number_reading(raw: str, unit: str | None, spec: dict) -> tuple[str, float | None]:
    """Как прочитать число из ячейки или строки бланка (raw — число как в бланке, без знака «<»):
    ("ok", значение) | ("multi", None) — несколько результатов подряд | ("ambiguous", None) — неясно, какое число.

    Границы — hard и soft показателя из config/analytes.yaml с учётом множителя единицы (как within_hard):
      * «1 210» (группы по три цифры через пробел) — одно число 1210, если оно в hard и (в soft или хотя бы одна
        группа сама по себе вне soft: тромбоциты «1 210» — 1 не похоже на тромбоциты);
      * в hard, но вне soft, а каждая группа в soft («Ферритин 45 108») — неясно: одно число или два результата;
      * вне hard, а первая группа в hard («Гемоглобин 141 102») — предыдущий и текущий результаты в строке;
      * «1.210», «1,210» — если 1210 в hard показателя, неясно, десятичная это точка или разделитель тысяч."""
    s = str(raw).strip()
    hard, soft = spec.get("hard"), spec.get("soft")
    if _THOUSANDS_SPLIT.search(s):
        parts = [p for p in _THOUSANDS_SPLIT.split(s) if p]
        grouped = float("".join(parts).replace(",", "."))
        groups = [float(p.replace(",", ".")) for p in parts]
        if not _within(hard, grouped, unit, spec):
            if _within(hard, groups[0], unit, spec):
                return "multi", None
            return "ok", grouped                    # дальше — «значение вне диапазона», как у любого числа
        if not _within(soft, grouped, unit, spec) and all(_within(soft, g, unit, spec) for g in groups):
            return "ambiguous", None
        return "ok", grouped
    if _THREE_DECIMALS.match(s):
        thousands = float(re.sub(r"[.,]", "", s))
        if _within(hard, thousands, unit, spec):
            return "ambiguous", None
    return "ok", float(s.replace(",", "."))


class Collector:
    """Результат разбора бланка: значения, нераспознанное (без текста бланка), замечания, нормы, известные
    неиспользуемые показатели. extended=False — прежний ответ text_parser (values, unrecognized, notes);
    известные неиспользуемые показатели тогда — «название показателя не распознано», как раньше.

    Несколько значений одного показателя: остаётся первое, если у следующего ранг не выше. Ранг — (единица
    распознана или не указана; метод СОЭ — Вестергрен): значение с нераспознанной единицей («Эритроциты 1 в п/зр»,
    «RDW 42 фл») уступает значению с допустимой единицей; СОЭ по Панченкову уступает СОЭ по Вестергрену."""

    def __init__(self, cfg: Config, *, extended: bool = False):
        self.cfg = cfg
        self.extended = extended
        self.values: dict[str, dict] = {}
        self.found_at: dict[str, int] = {}
        self.unrecognized: list[tuple[tuple[int, int], str]] = []
        self.notes: list[str] = []
        self.ranges: dict[str, dict] = {}
        self.ignored: list[str] = []
        self.skipped = 0
        self.date_hint: str | None = None           # дата столбца результата в таблице динамики (ISO)
        self._rank: dict[str, tuple[int, int]] = {}
        self._value_notes: dict[str, list[str]] = {}
        self._methods: dict[str, set[str]] = {}
        self._method: dict[str, str | None] = {}

    def name(self, code: str) -> str:
        return self.cfg.analytes[code].get("name_ru", code)

    def remember(self, order: tuple[int, int], where: str, what: str) -> None:
        """Нераспознанная строка: только место и распознанные части (без текста бланка)."""
        if len(self.unrecognized) < MAX_UNRECOGNIZED:
            short = f"{where}: {what}"
            if len(short) > UNRECOGNIZED_WIDTH:
                short = short[:UNRECOGNIZED_WIDTH - 1] + "…"
            self.unrecognized.append((order, short))
        else:
            self.skipped += 1

    def ignore(self, display: str, order: tuple[int, int], where: str, quiet: bool = False) -> None:
        if self.extended:
            if display not in self.ignored:
                self.ignored.append(display)
        elif not quiet:
            self.remember(order, where, "название показателя не распознано")

    def multiple(self, code: str, order: tuple[int, int], where: str) -> None:
        """В строке несколько результатов («141 102 г/л», «128 → 135», столбцы-даты без ясного последнего):
        какой из них текущий — не понять, значение не берётся."""
        name = self.name(code)
        self.remember(order, where, f"{name} — несколько результатов")
        self.notes.append(f"{name}: в строке несколько результатов (например, предыдущий и текущий) — значение "
                          "не взято. Проверьте бланк и введите значение вручную.")

    def ambiguous(self, code: str, order: tuple[int, int], where: str) -> None:
        """Число можно прочитать двояко («1.210»: 1,21 или 1210; «45 108»: одно число или два) — не берётся."""
        name = self.name(code)
        self.remember(order, where, f"{name} — проверьте значение")
        self.notes.append(f"{name}: число в бланке можно прочитать по-разному (разделитель тысяч или десятичный "
                          "знак, одно число или два) — значение не взято. Проверьте и введите вручную.")

    def foreign(self, code: str, order: tuple[int, int], where: str) -> None:
        """Значение в единицах анализа мочи («в п/зр», «кл/мкл») под названием показателя крови — не берётся."""
        name = self.name(code)
        self.remember(order, where, f"{name} — единица анализа мочи")
        self.notes.append(f"{name}: значение в единицах «в поле зрения» или «клеток/мкл» — это анализ мочи, "
                          "а не крови. Значение не взято.")

    def add(self, code: str, value: float, *, sign: str | None, unit: str | None, unit_unknown: bool,
            rng: tuple[float | None, float | None] | None, order: tuple[int, int], where: str,
            lineno: int | None = None, raw: str | None = None, method: str | None = None) -> bool:
        """Значение показателя. raw — число как в бланке («1 210», «1.210»): читается number_reading; method —
        метод СОЭ (esr_method). Возвращает True, если значение принято (первым или вместо значения ниже рангом)."""
        spec = self.cfg.analytes[code]
        name = self.name(code)
        if raw is not None:
            how, read = number_reading(raw, unit, spec)
            if how == "multi":
                self.multiple(code, order, where)
                return False
            if how == "ambiguous":
                self.ambiguous(code, order, where)
                return False
            value = read
        if not math.isfinite(value) or not within_hard(value, unit, spec):
            self.remember(order, where, f"{name} — значение вне диапазона")
            self.notes.append(f"{name}: значение в строке не похоже на этот показатель — строка пропущена.")
            return False
        if method:
            self._methods.setdefault(code, set()).add(method)
        rank = (0 if unit is None and unit_unknown else 1, 1 if method == "westergren" else 0)
        if code in self.values:
            old = self._rank[code]
            both_methods = {"westergren", "panchenkov"} <= self._methods.get(code, set())
            if rank <= old:
                if not both_methods:                # о двух методах СОЭ — отдельное замечание (_method_notes)
                    self.notes.append(f"{name} встречается несколько раз: взято первое значение.")
                return False
            self._drop(code)
            if rank[0] > old[0]:
                self.notes.append(f"{name} встречается несколько раз: взято значение с распознанной единицей.")
            elif not both_methods:
                self.notes.append(f"{name} встречается несколько раз: взято значение по Вестергрену.")
        self.values[code] = {"value": value, "unit": unit}
        self._rank[code] = rank
        self._method[code] = method
        if lineno is not None:
            self.found_at[code] = lineno
        own: list[str] = []
        if sign:
            own.append(f"{name}: в бланке значение со знаком «{sign.strip()}» — взято само число.")
        if unit is None and unit_unknown:
            # Сам текст «единицы» не показывается: за числом в бланке бывает фамилия или номер телефона.
            own.append(f"{name}: единица не распознана — принята {spec.get('unit_ru', '')}. Проверьте.")
        elif unit is None and spec.get("unit_ambiguous"):
            own.append(f"{name}: единица не указана — принята {spec.get('unit_ru', '')}. Проверьте.")
        self.notes.extend(own)
        self._value_notes[code] = own
        if rng is not None:
            self._add_range(code, rng, unit, unit_unknown)
        return True

    def _drop(self, code: str) -> None:
        """Убирает принятое значение (его заменит значение выше рангом): значение, норму, строку и его замечания."""
        self.values.pop(code, None)
        self.found_at.pop(code, None)
        self.ranges.pop(code, None)
        for note in self._value_notes.pop(code, []):
            if note in self.notes:
                self.notes.remove(note)

    def _add_range(self, code: str, rng: tuple[float | None, float | None], unit: str | None,
                   unit_unknown: bool) -> None:
        """Норма с бланка -> reference_ranges в канонической единице (тот же множитель, что у значения).
        Единица неизвестна или не указана у показателя с несколькими единицами — норму не берём: её нельзя
        пересчитать надёжно, а референсы лаборатории на странице важнее референсов проекта."""
        spec = self.cfg.analytes[code]
        if unit_unknown or (unit is None and spec.get("unit_ambiguous")) or code in self.ranges:
            return
        k = unit_factor(spec, unit)
        lo = None if rng[0] is None else round(rng[0] * k, 6)
        hi = None if rng[1] is None else round(rng[1] * k, 6)
        hard_hi = float((spec.get("hard") or [0, math.inf])[1])
        if lo is None and hi is None:
            return
        if any(x is not None and (x < 0 or x > hard_hi) for x in (lo, hi)):
            return                                      # число не похоже на норму этого показателя
        if lo is not None and hi is not None and lo > hi:
            return
        self.ranges[code] = {"low": lo, "high": hi, "unit": spec.get("unit_ru") or spec.get("unit")}

    def _method_notes(self) -> list[str]:
        """СОЭ: какой метод взят. Пороги норм СОЭ в проекте — по методу Вестергрена (референтный метод ICSH)."""
        if "ESR" not in self.values:
            return []
        name, kept, seen = self.name("ESR"), self._method.get("ESR"), self._methods.get("ESR", set())
        if kept == "westergren" and "panchenkov" in seen:
            return [f"{name}: в бланке два метода — взято значение по Вестергрену (референтный метод)."]
        if kept == "panchenkov":
            return [(f"{name}: метод Панченкова — при высоких значениях он расходится с методом Вестергрена, "
                     "по которому заданы нормы. Проверьте значение.")]
        return []

    def result(self, *, date: str | None = None, line_numbers: bool = False) -> dict:
        notes = list(dict.fromkeys([*self.notes, *self._method_notes()]))   # одинаковые замечания — один раз
        if self.skipped:
            notes.append(f"Ещё {self.skipped} нераспознанных строк не показаны.")
        if not self.values:
            notes.append("Показатели не найдены: проверьте, что в строках есть название анализа и число.")
        out: dict[str, Any] = {"values": self.values,
                               "unrecognized": [t for _, t in sorted(self.unrecognized, key=lambda x: x[0])],
                               "notes": notes}
        if self.extended:
            out["reference_ranges"] = self.ranges
            out["ignored"] = self.ignored
            out["date"] = date
        if line_numbers:
            out["lines"] = dict(self.found_at)
        return out
