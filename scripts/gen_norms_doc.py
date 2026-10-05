#!/usr/bin/env python3
"""Создаёт docs/norms.md из config/norms_ru.yaml, чтобы документ не расходился с конфигурацией.

В таблицу попадает каждый числовой порог конфигурации: показатель, порог, единица, источник (название и ссылка),
статус проверки, происхождение и ответ на вопрос «применяется ли порог сейчас».

«Применяется сейчас» определяется так же, как в коде (src/deficitlens_core/rules.py, norm_value):
  * запись с origin: foreign при policy.use_foreign: false — не применяется, если её нет в policy.foreign_allowed;
  * порог со значением null — не задан, показатель не оценивается;
  * ключ, которого нет ни в одном файле src/deficitlens_core, — справочное значение, код его не читает.

Запуск:  python3 scripts/gen_norms_doc.py [--config config] [--out docs/norms.md] [--check]
  --check — ничего не писать, а сравнить с существующим файлом (код возврата 1, если файл устарел).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# Ключ записи в norms_ru.yaml -> (код показателя в analytes.yaml для единицы, название по-русски).
ENTRY = {
    "crp": ("CRP", "С-реактивный белок (СРБ)"),
    "tsat": ("TSAT", "Насыщение трансферрина железом (НТЖ)"),
    "serum_iron": ("serum_iron", "Сывороточное железо"),
    "tibc": ("TIBC", "ОЖСС"),
    "transferrin": ("transferrin", "Трансферрин"),
    "ret_he": ("Ret_He", "Гемоглобин в ретикулоцитах (Ret-He)"),
    "stfr": ("sTfR", "Растворимые рецепторы трансферрина (sTfR)"),
    "vitamin_b12": ("vitamin_B12", "Витамин B12 общий"),
    "active_b12": ("active_B12", "Активный B12 (голотранскобаламин)"),
    "homocysteine": ("homocysteine", "Гомоцистеин"),
    "mma": ("MMA", "Метилмалоновая кислота (ММК)"),
    "folate": ("folate", "Фолаты сыворотки"),
    "vitamin_b6": ("vitamin_B6", "Витамин B6"),
    "copper": ("copper", "Медь сыворотки"),
    "ceruloplasmin": ("ceruloplasmin", "Церулоплазмин"),
    "egfr": ("eGFR", "Расчётная СКФ (рСКФ)"),
    "tsh": ("TSH", "ТТГ"),
}
# Ключ порога -> (подпись, знак сравнения).
THRESHOLD = {
    "deficiency_below": ("дефицит", "<"),
    "low_below": ("снижено", "<"),
    "high_above": ("повышено", ">"),
    "inflammation_above": ("признак воспаления", ">"),
    "reduced_below": ("снижена", "<"),
    "severe_below": ("выраженное снижение", "<"),
    "marked_above": ("выраженное повышение", ">"),
}
ZONE = {"gray_zone": "серая зона", "borderline": "пограничные значения"}
STATUS_RU = {
    "verified": "проверено по первоисточнику",
    "secondary": "по зеркалу текста КР (вторичный источник)",
    "needs_medical_expert": "нужен врач: рабочее значение",
}
INDEX = {
    "mcv_micro_below": ("MCV", "MCV: микроцитоз", "<"),
    "mcv_macro_above": ("MCV", "MCV: макроцитоз", ">"),
    "mch_low_below": ("MCH", "MCH: гипохромия", "<"),
    "rdw_high_above": ("RDW", "RDW: повышен", ">"),
    "mentzer_thalassemia_like_below": (None, "Индекс Ментцера (MCV / RBC), только при микроцитозе", "<"),
}
# Раздел history (анализ истории, src/deficitlens_core/history.py): подраздел -> название события или срока.
HISTORY = {
    "hb_drop": "снижение гемоглобина (от медианы предыдущих анализов в окне или от предыдущего анализа)",
    "ferritin_drop": "снижение ферритина (и переход порогов ferritin.deficiency_below)",
    "mcv_drift": "сдвиг MCV (от медианы предыдущих анализов в окне)",
    "persistent_signal": "сигнал скрытого дефицита подряд (разные даты)",
    "screening_repeat": "риск по скринингу ОАК повторяется, ферритин не сдан",
    "stale": "«давно» (полнота картины, ОАК давно не сдавался)",
    "flat": "«без изменений» для направления ряда",
    "limits": "ограничения объёма (не медицинский порог)",
}
# Ключ порога раздела history -> (подпись, знак, единица).
HISTORY_LEAF = {
    "min_drop_g_l": ("снижение не меньше", "≥", "г/л"),
    "min_drop_pct": ("снижение не меньше", "≥", "% от раннего значения"),
    "min_change_fl": ("изменение не меньше", "≥", "фл"),
    "window_months": ("ранний анализ не дальше от последнего", "≤", "мес"),
    "min_records": ("анализов не меньше", "≥", "анализов"),
    "max_gap_months": ("разрыв между соседними анализами серии не больше", "≤", "мес"),
    "crossing_beyond_flat": ("переход порога — только при снижении больше «шума» history.flat", "", "—"),
    "max_points": ("точек в ряду на показатель не больше (последние)", "≤", "точек"),
    "max_records": ("записей в разборе не больше (последние)", "≤", "записей"),
    "risks": ("учитываемые уровни риска", "", "—"),
    "months": ("последний анализ старше", ">", "мес"),
    "ferritin_low_months": ("ферритин ниже порога практики: последний старше", ">", "мес"),
    "default_pct": ("|последнее − первое| меньше", "<", "% от первого значения"),
}
SERVICE_KEYS = {"version", "policy", "sources", "no_reference_yet", "reference_ranges", "local_references"}
NOT_THRESHOLD_KEYS = {"source", "sources", "status", "origin", "note", "ru_note", "ru_label", "age_note",
                      "inflammation_note", "formula_source", "mentzer_source", "gray_zone_inclusive",
                      "pregnancy_note", "labels"}


def num(x) -> str:
    """Число по-русски: десятичная запятая, без лишних нулей."""
    if isinstance(x, bool) or x is None:
        return str(x)
    f = float(x)
    return (str(int(f)) if f.is_integer() else f"{f:g}").replace(".", ",")


class Doc:
    def __init__(self, cfg_dir: Path):
        self.norms = yaml.safe_load((cfg_dir / "norms_ru.yaml").read_text(encoding="utf-8"))
        self.analytes = yaml.safe_load((cfg_dir / "analytes.yaml").read_text(encoding="utf-8"))["analytes"]
        self.use_foreign = bool((self.norms.get("policy") or {}).get("use_foreign", False))
        self.foreign_allowed = [str(k) for k in ((self.norms.get("policy") or {}).get("foreign_allowed") or [])]
        self.sources = self.norms.get("sources", {})
        code = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "src" / "deficitlens_core").rglob("*.py")))
        self.code = code
        self.rows: list[list[str]] = []
        self.rendered: set[tuple] = set()

    # --- столбцы -----------------------------------------------------------------------------
    def unit(self, analyte: str | None) -> str:
        return self.analytes.get(analyte, {}).get("unit_ru", "—") if analyte else "—"

    def source(self, key: str | None) -> str:
        if not key:
            return "не указан"
        s = self.sources.get(key)
        if not s:
            return f"`{key}` (нет в разделе sources)"
        url = (s.get("url") or "").strip()
        return f"{s['title']} — {url}" if url else f"{s['title']} — ссылки в конфигурации нет"

    def origin(self, entry: dict, source_key: str | None) -> str:
        if entry.get("origin"):
            return f"`{entry['origin']}`" + (" — зарубежное национальное руководство" if entry["origin"] == "foreign" else "")
        k = source_key or ""
        derived = ("ВОЗ" if k.startswith("who_") else "Россия: КР Минздрава" if k.startswith("kr_")
                   else "общепринятый диапазон (проект)" if k == "lab_reference"
                   else "решение команды проекта" if k == "project"
                   else "публикация" if k else "не указано")
        return f"не задано; по источнику: {derived}"

    def applied(self, entry: dict, leaf_key: str, value, key: str = "") -> str:
        if entry.get("origin") == "foreign" and not self.use_foreign:
            if key not in self.foreign_allowed:
                return "**нет** — зарубежный порог, `policy.use_foreign: false`"
            if value is not None and leaf_key in self.code:
                return "да — зарубежный порог, разрешён `policy.foreign_allowed`"
        if value is None:
            return "**нет** — порог не задан"
        if leaf_key not in self.code:
            return "**нет** — справочное значение, код его не читает"
        return "да"

    def add(self, path: tuple, name: str, threshold: str, unit: str, entry: dict, leaf_key: str, value,
            source_key: str | None, status: str | None) -> None:
        self.rendered.add(path)
        self.rows.append([name, threshold, unit, self.source(source_key), STATUS_RU.get(status, status or "не указан"),
                          self.origin(entry, source_key), self.applied(entry, leaf_key, value, str(path[0]))])

    # --- разделы конфигурации ------------------------------------------------------------------
    def severity(self, bands: dict) -> str:
        mild, mod = bands["mild"], bands["moderate"]
        return (f"лёгкая: от {num(mild[0])} до {num(mild[1])}; умеренная: от {num(mod[0])} до {num(mod[1])}; "
                f"тяжёлая: < {num(bands['severe_below'])} (нижняя граница входит, верхняя — нет)")

    def hemoglobin(self) -> None:
        e = self.norms["hemoglobin"]
        u, src, st = self.unit("hemoglobin"), e.get("source"), e.get("status")
        sex = {"F": "женщины", "M": "мужчины"}
        for s, title in sex.items():
            v = e["anemia_below"][s]
            self.add(("hemoglobin", "anemia_below", s), f"Гемоглобин: анемия, {title} (не беременные)",
                     f"< {num(v)}", u, e, "anemia_below", v, src, st)
        for s, title in sex.items():
            self.add(("hemoglobin", "severity", s), f"Гемоглобин: тяжесть анемии, {title}",
                     self.severity(e["severity"][s]), u, e, "severity", 1, src, st)
        tri = {1: "I триместр", 2: "II триместр", 3: "III триместр", "unknown": "триместр неизвестен"}
        for t, title in tri.items():
            v = e["pregnancy"]["anemia_below"][t]
            self.add(("hemoglobin", "pregnancy", "anemia_below", t), f"Гемоглобин: анемия при беременности, {title}",
                     f"< {num(v)}", u, e, "anemia_below", v, src, st)
        for t, title in tri.items():
            self.add(("hemoglobin", "pregnancy", "severity", t), f"Гемоглобин: тяжесть при беременности, {title}",
                     self.severity(e["pregnancy"]["severity"][t]), u, e, "severity", 1, src, st)
        self.add(("hemoglobin", "age_max"), "Гемоглобин: возраст, до которого пороги ВОЗ применяются напрямую",
                 f"≤ {num(e['age_max'])}", "лет", e, "age_max", e["age_max"], src, st)

    def urgent(self) -> None:
        e = self.norms["urgent"]
        src, st = e.get("source"), e.get("status")
        labels, srcs = e.get("labels") or {}, e.get("sources") or {}

        def name(base: str, key: str) -> str:
            return f"{base} — {labels[key]}" if labels.get(key) else base

        hb = e["hemoglobin_below"]
        hb_src = srcs.get("hemoglobin", src)
        self.add(("urgent", "hemoglobin_below", "default"), name("Блок «срочно»: гемоглобин", "hemoglobin"),
                 f"< {num(hb['default'])}", self.unit("hemoglobin"), e, "hemoglobin_below", hb["default"], hb_src, st)
        self.add(("urgent", "hemoglobin_below", "pregnant"), name("Блок «срочно»: гемоглобин при беременности", "hemoglobin"),
                 f"< {num(hb['pregnant'])}", self.unit("hemoglobin"), e, "hemoglobin_below", hb["pregnant"], hb_src, st)
        self.add(("urgent", "wbc_below"), name("Блок «срочно»: лейкоциты", "wbc"), f"< {num(e['wbc_below'])}",
                 self.unit("WBC"), e, "wbc_below", e["wbc_below"], srcs.get("wbc", src), st)
        self.add(("urgent", "platelets_below"), name("Блок «срочно»: тромбоциты", "platelets"),
                 f"< {num(e['platelets_below'])}", self.unit("platelets"), e, "platelets_below", e["platelets_below"],
                 srcs.get("platelets", src), st)

    def ferritin(self) -> None:
        e = self.norms["ferritin"]
        u, srcs, sts = self.unit("ferritin"), e.get("sources", {}), e.get("status", {})
        d = e["deficiency_below"]
        self.add(("ferritin", "deficiency_below", "who"), "Ферритин: дефицит железа, набор порогов «ВОЗ»",
                 f"< {num(d['who'])}", u, e, "deficiency_below", d["who"], srcs.get("who"), sts.get("who"))
        self.add(("ferritin", "deficiency_below", "ru"), f"Ферритин: набор порогов «практика РФ» ({e.get('ru_label', '')})",
                 f"< {num(d['ru'])}", u, e, "deficiency_below", d["ru"], srcs.get("ru"), sts.get("ru"))
        self.add(("ferritin", "inflammation_below"), f"Ферритин при воспалении ({e.get('inflammation_note', '')})",
                 f"< {num(e['inflammation_below'])}", u, e, "inflammation_below", e["inflammation_below"],
                 srcs.get("inflammation"), sts.get("inflammation"))
        t = e["treatment_target"]
        self.add(("ferritin", "treatment_target"), "Ферритин: цель лечения", f"> {num(t[0])}–{num(t[1])}", u, e,
                 "treatment_target", t, srcs.get("target"), sts.get("target"))

    def indices(self) -> None:
        e = self.norms["indices"]
        for key, (analyte, title, sign) in INDEX.items():
            if key not in e:
                continue
            src = e.get("mentzer_source") if key.startswith("mentzer") else e.get("source")
            self.add(("indices", key), title, f"{sign} {num(e[key])}", self.unit(analyte), e, key, e[key], src,
                     e.get("status"))

    def flat(self, key: str) -> None:
        e = self.norms[key]
        analyte, title = ENTRY.get(key, (None, f"`{key}`"))
        u, src, st = self.unit(analyte), e.get("source"), e.get("status")
        for k, v in e.items():
            if k in NOT_THRESHOLD_KEYS:
                continue
            if k in THRESHOLD:
                label, sign = THRESHOLD[k]
                thr = "не задан" if v is None else f"{sign} {num(v)}"
                self.add((key, k), f"{title}: {label}", thr, u, e, k, v, src, st)
            elif k in ZONE:
                both = " (обе границы входят)" if e.get("gray_zone_inclusive") and k == "gray_zone" else \
                    " (нижняя граница входит, верхняя — нет)"
                self.add((key, k), f"{title}: {ZONE[k]}", f"{num(v[0])}–{num(v[1])}{both}", u, e, k, v, src, st)
            else:                       # незнакомый ключ: показываем как есть, чтобы ничего не потерять
                self.add((key, k), f"{title}: `{k}`", str(v), u, e, k, v, src, st)

    def history(self) -> None:
        """Раздел history: пороги событий динамики, сроки «давно» и «шум» (по строке на число)."""
        e = self.norms["history"]
        src, st = e.get("source"), e.get("status")
        for sub, spec in e.items():
            if sub in NOT_THRESHOLD_KEYS or not isinstance(spec, dict):
                continue
            title = f"История: {HISTORY.get(sub, f'`{sub}`')}"
            for k, v in spec.items():
                if k in ("pct", "abs") and isinstance(v, dict):      # «шум» по показателям
                    for analyte, x in v.items():
                        name = self.analytes.get(analyte, {}).get("name_ru", analyte)
                        unit = self.unit(analyte) if k == "abs" else "% от первого значения"
                        self.add(("history", sub, k, analyte), f"{title}: {name}", f"< {num(x)}", unit, e, k, x,
                                 src, st)
                elif k in HISTORY_LEAF:
                    label, sign, unit = HISTORY_LEAF[k]
                    if isinstance(v, list):
                        thr = ", ".join(map(str, v))
                    elif isinstance(v, bool):           # правило «включено / выключено»
                        thr = "да" if v else "нет"
                    else:
                        thr = f"{sign} {num(v)}"
                    self.add(("history", sub, k), f"{title}: {label}", thr, unit, e, k, v, src, st)
                else:                   # незнакомый ключ: показываем как есть, чтобы ничего не потерять
                    self.add(("history", sub, k), f"{title}: `{k}`", str(v), "—", e, k, v, src, st)

    def build(self) -> None:
        for key in self.norms:
            if key in SERVICE_KEYS:
                continue
            handler = {"hemoglobin": self.hemoglobin, "urgent": self.urgent, "ferritin": self.ferritin,
                       "indices": self.indices, "history": self.history}.get(key)
            if handler:
                handler()
            elif isinstance(self.norms[key], dict):
                self.flat(key)

    # --- текст -------------------------------------------------------------------------------
    def notes(self) -> list[str]:
        out = []
        for key, e in self.norms.items():
            if key in SERVICE_KEYS or not isinstance(e, dict):
                continue
            title = ENTRY.get(key, (None, {"hemoglobin": "Гемоглобин", "urgent": "Блок «срочно»",
                                           "ferritin": "Ферритин", "indices": "Индексы ОАК",
                                           "history": "Анализ истории"}.get(key, key)))[1]
            for k in ("note", "ru_note", "age_note", "pregnancy_note"):
                if e.get(k):
                    out.append(f"- **{title}.** {str(e[k]).rstrip('.')}.")
        return out

    def render(self) -> str:
        n = self.norms
        pol = n.get("policy", {})
        refs = n.get("reference_ranges") or {}
        L = ["# Пороги и источники", "",
             "Этот файл создаёт `scripts/gen_norms_doc.py` из `config/norms_ru.yaml`. Правьте конфигурацию и "
             "запускайте скрипт заново; сам файл руками не правьте.", "",
             f"Версия конфигурации: `{n.get('version')}`.", "",
             "Это пороги показа и правил. Границы, которые выучила модель, здесь не хранятся: они лежат в "
             "`models/` и подписываются как «граница по данным набора», а не как порог ВОЗ.", "",
             "Все значения — черновик до проверки врачом команды. Статус «проверено по первоисточнику» значит "
             "только то, что число сверено с текстом источника; уместность порога для конкретного пациента "
             "решает врач.", "",
             "## Правило источников", "",
             f"Решение: {pol.get('decided', 'не записано')}.", "",
             "- Пороги берутся из российских документов (клинические рекомендации Минздрава, приказы), "
             "из текста кейса и из руководств ВОЗ. ВОЗ прямо назван в кейсе.",
             "- Пороги из зарубежных национальных руководств (NICE, BCSH) по умолчанию не применяются. Такие записи "
             "помечены `origin: foreign`.",
             f"- Сейчас `policy.use_foreign: {str(self.use_foreign).lower()}` — глобально зарубежные пороги "
             + ("включены." if self.use_foreign else "выключены."),
             "- Исключение — записи из `policy.foreign_allowed`: "
             + (", ".join(ENTRY.get(k, (None, k))[1] for k in self.foreign_allowed) or "нет") + ". "
             "По решению капитана 04.10.2026 (`docs/review/medic_decisions.md`, п. 10; пересмотр правки 1) B12, "
             "активный B12 и гомоцистеин оцениваются числом по NICE NG239 с подписью «зарубежный источник; в КР РФ "
             "числового порога нет»: общий B12 < 180 пг/мл (= нг/л) — дефицит, 180–350 — серая зона (флаг «B12 требует "
             "функционального подтверждения» и досдать активный B12 и гомоцистеин); активный B12 < 25 пмоль/л — дефицит, "
             "25–70 — серая зона; гомоцистеин > 15 мкмоль/л — повышен. Ожидает подтверждения врачом команды.",
             "- Метилмалоновая кислота — без диагностического порога: только референс «обычно < 0,30 мкмоль/л, "
             "зависит от метода».",
             "- Если запись B12 убрать из `foreign_allowed`, «пограничный B12» определяется без порога: общий B12 сдан, "
             "функциональные маркеры (активный B12, ММК, гомоцистеин) не сданы, а модель не уверена. Тогда движок "
             "ставит флаг `b12_gray_zone` и предлагает активный B12 или гомоцистеин (`engine.py`, функция `_b12_flag`; "
             "полоса неуверенности — `config/class_mapping.yaml → model_flags.b12_uncertain`).",
             "- Выключенный показатель в таблице значений получает статус «неизвестно» и подпись "
             "«числового порога в КР РФ нет; оценка по референсу лаборатории».", "",
             "## Таблица порогов", "",
             "| Показатель | Порог | Единица | Источник | Статус проверки | Происхождение (`origin`) | Применяется сейчас |",
             "|---|---|---|---|---|---|---|"]
        L += ["| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |" for row in self.rows]
        L += ["",
              "Поле `origin` в конфигурации задано только у зарубежных записей. Для остальных строк происхождение "
              "выведено из ключа источника.", "",
              "«Применяется сейчас: нет — справочное значение» значит, что число записано в конфигурации, но ни одно "
              "правило его не читает.", "",
              "## Примечания из конфигурации", ""]
        L += self.notes()
        L += ["",
              "## Чего нет в российских клинических рекомендациях", "",
              "- В КР «Железодефицитная анемия» (ID 669) нет числового порога ферритина для диагностики у взрослых. "
              "Число 30 в КР относится к латентному дефициту у беременных и к контролю восполнения депо.",
              "- В КР «Витамин-B12-дефицитная анемия» (ID 536) нет числовых порогов B12, активного B12, ММК "
              "и гомоцистеина.",
              "- Тексты КР сверены по зеркалам, а не по рубрикатору Минздрава: рубрикатор не отдаёт текст без "
              "браузера. Номера и год КР официальным источником не подтверждены. Подробности — "
              "`docs/facts/FACTS.md` и `docs/facts/FACTS_recheck.md`.", "",
              "## Референсы проекта по умолчанию", "",
              "Решение капитана 04.10.2026 (`docs/review/medic_decisions.md`, п. 10), таблица "
              "`docs/facts/reference_table.md`. Референс — это «норма» для статусов таблицы значений («в пределах / "
              "ниже / выше референса») и для чек-листа исключений (гемолиз, медь). Диагностические пороги выше им не "
              "меняются: порог решает свою сторону, значение между референсом и порогом — «пограничное», а где порога "
              "нет — «ниже / выше референса». Граница входит в референс. Статус всех строк — "
              f"«{STATUS_RU.get(refs.get('status'), refs.get('status'))}». Единицы — канонические единицы сервиса; "
              "фолаты пересчитаны из нмоль/л таблицы в нг/мл (÷ 2,266). При беременности референсы проекта "
              + ("применяются." if refs.get("apply_in_pregnancy") else "не применяются (по умолчанию до ответа врача)."),
              "",
              "| Показатель | Код | Единица | Женщины | Мужчины | Первоисточник (лист «Источники») |",
              "|---|---|---|---|---|---|"]

        def rng(pair) -> str:
            if not pair:
                return "—"
            lo, hi = pair
            if lo is not None and hi is not None:
                return f"{num(lo)}–{num(hi)}"
            return f"≥ {num(lo)}" if lo is not None else f"≤ {num(hi)}"

        for code, entry in (refs.get("ranges") or {}).items():
            info = self.analytes.get(code, {})
            f_rng, m_rng = rng(entry.get("F", entry.get("range"))), rng(entry.get("M", entry.get("range")))
            primary = self.sources.get(entry.get("primary"), {}).get("title") if entry.get("primary") else None
            L.append(f"| {info.get('name_ru', code)} | `{code}` | {info.get('unit_ru', '—')} | {f_rng} | {m_rng} | "
                     f"{primary or 'в таблице не указан'} |")
        L += ["",
              "## Локальные референсы лаборатории", "",
              "Референсы конкретной лаборатории заменяют референсы проекта по умолчанию для своих показателей. "
              "Два способа: набор в `config/norms_ru.yaml → local_references.<имя>` (выбирается полем запроса "
              "`lab_reference` — тем же, что выбирает справочник ОАК для скрининга) или поле запроса "
              "`reference_ranges` (`{\"LDH\": {\"low\": 135, \"high\": 225, \"unit\": \"Ед/л\"}}`; единица — любая "
              "допустимая для показателя, пересчитывается в каноническую). При любом локальном референсе в отчёте "
              "врачу (первая строка раздела «Ограничения»), в `limitations` и в поле ответа `references.warning` — "
              "предупреждение «Использованы референсные интервалы лаборатории. Они имеют приоритет над значениями "
              "по умолчанию проекта; проверьте единицы, метод и возрастно-половую группу.» "
              "(`config/texts_ru/doctor.yaml → references.local_warning`).", "",
              f"Наборов в конфигурации сейчас: {len(n.get('local_references') or {})}.", "",
              "## Жёсткие рамки ввода", "",
              "Жёсткие рамки (`config/analytes.yaml → hard`) — физически невозможные значения: за ними ответ 422, "
              "подтвердить значение нельзя. Это не референс. Рамки редактируются только в конфигурации "
              "(`config/analytes.yaml`, затем перезапуск сервиса); на странице настройки нет.", "",
              "## Показатели без порога", ""]
        if n.get("no_reference_yet"):
            L += ["Для этих показателей оценка «норма / не норма» не выводится (`no_reference_yet`). В отчёте стоит "
                  "«оценка по референсу лаборатории».", ""]
            for a in n.get("no_reference_yet", []):
                info = self.analytes.get(a, {})
                L.append(f"- {info.get('name_ru', a)} (`{a}`), {info.get('unit_ru', 'единица не указана')}")
        else:
            L += ["Список `no_reference_yet` пуст: у всех показателей таблицы есть референс проекта по умолчанию."]
        unset = [r[0] for r in self.rows if "порог не задан" in r[6]]
        if unset:
            L += ["", "Порог в конфигурации есть, но значение не задано:", ""] + [f"- {x}" for x in unset]
        L += ["",
              "## Источники", "",
              "| Ключ | Название | Ссылка |", "|---|---|---|"]
        for k, s in self.sources.items():
            L.append(f"| `{k}` | {s.get('title', '')} | {(s.get('url') or '').strip() or 'нет'} |")
        L += ["",
              "Номера страниц не приводятся намеренно: они не проверялись. Ссылки на КР ведут на главную страницу "
              "рубрикатора, а не на документ.", ""]
        return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(ROOT / "config"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "norms.md"))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    doc = Doc(Path(a.config))
    doc.build()
    text = doc.render()
    out = Path(a.out)
    if a.check:
        if not out.exists() or out.read_text(encoding="utf-8") != text:
            print(f"{out} устарел: запустите python3 scripts/gen_norms_doc.py", file=sys.stderr)
            return 1
        print(f"{out} соответствует конфигурации")
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"записано: {out} ({len(doc.rows)} строк порогов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
