"""«Следующий анализ»: что досдать, почему и сколько это стоит.

Порядок рекомендаций:
  1) ферритин (и СРБ) по результату скрининга — если гемоглобин в норме, а риск скрытого дефицита железа повышен;
  2) анализы, обязательные по правилам (rules.mandatory_tests и флаги движка): например, при анемии без
     ферритина — показатели обмена железа по клиническим рекомендациям;
  3) анализы, которые лучше всего различают оставшиеся варианты: ожидаемое уменьшение энтропии по таблицам
     модели (в битах), делённое на (1 + цена / медианная цена) — «польза на рубль».
Пункт 3 включается, только когда есть что уточнять (анемия, сигнал правила или модели по сданным анализам):
здоровому человеку без сигналов лишние анализы не предлагаются. Цены — config/prices.yaml (Москва, 03.10.2026).
Ограничения (ревизия содержания 05.10.2026, п. 9): весь список — не длиннее MAX_ITEMS (правила идут первыми в своём
порядке; полный перечень непроверенного врач видит в чек-листе исключений); по приросту информации — не больше
MAX_INFORMATION пунктов и только пока в списке есть место. НТЖ не предлагается
вместе с железом и ОЖСС: оно из них рассчитывается (normalize.py); если НТЖ требует правило, железо и ОЖСС
по приросту информации не добавляются.
"""
from __future__ import annotations

from statistics import median
from typing import Optional

import numpy as np

from .config import Config
from .schemas import HiddenDeficiency, NextTest

MAX_ITEMS = 5          # сколько анализов предлагаем всего (сначала правила, затем по приросту информации)
MAX_INFORMATION = 3    # не больше стольких анализов «по приросту информации»
MIN_GAIN_BITS = 0.03   # ниже этого прироста информации анализ не предлагаем

# Расчётные показатели и «двойники» заказываются через исходный анализ:
# рСКФ считается по креатинину; ЛЖСС (UIBC) лаборатории выдают вместе с ОЖСС.
ORDER_AS = {"eGFR": "creatinine", "UIBC": "TIBC"}
# НТЖ = железо сыворотки / ОЖСС × 100 (normalize.py): вместе с этой парой НТЖ отдельно не предлагаем.
TSAT_PARTS = {"serum_iron", "TIBC"}


def _gain_text(gain: float) -> str:
    return f"{gain:.2f}".replace(".", ",")


def _redundant(analyte: str, chosen: set[str], rule_chosen: set[str]) -> bool:
    """Анализ «по приросту информации» лишний: НТЖ при выбранных железе и ОЖСС, или железо / ОЖСС при НТЖ по правилу."""
    if analyte == "TSAT":
        return TSAT_PARTS <= chosen
    return analyte in TSAT_PARTS and "TSAT" in rule_chosen


def rank_next_tests(*, norm, hidden: HiddenDeficiency, mandatory_tests: list[dict], case_model,
                    x_row: Optional[np.ndarray], cfg: Config, allow_information: bool) -> list[NextTest]:
    available = set(norm.values)  # сданные и производные (расчётные НТЖ и рСКФ повторно не предлагаем)
    out: list[NextTest] = []
    seen: set[str] = set()

    def add(analyte: str, reason: str, kind: str, gain: float | None = None, source: str | None = None) -> None:
        if analyte in available or analyte in seen or analyte not in cfg.analytes or len(out) >= MAX_ITEMS:
            return
        price, is_available = cfg.price(analyte)
        out.append(NextTest(analyte=analyte, name_ru=cfg.name_ru(analyte), reason=reason, kind=kind,
                            information_gain=gain, price_rub=price, available=is_available, source=source))
        seen.add(analyte)

    # 1) Скрининг: гемоглобин в норме, ферритин не сдан, риск выше порога направления.
    scr = hidden.screening
    if scr is not None and scr.applicable and scr.recommend_ferritin:
        add("ferritin",
            f"Гемоглобин в норме, но по общему анализу крови риск скрытого дефицита железа {scr.risk_ru}. "
            "Ферритин покажет запас железа.",
            "rule", source="скрининг по общему анализу крови (модель проверена на данных NHANES)")
        add("CRP", "Нужен, чтобы правильно прочитать ферритин: при воспалении ферритин завышается.",
            "rule", source=cfg.source_title("who_ferritin_2020"))

    # 2) Обязательные по правилам и флагам.
    for item in mandatory_tests:
        add(item["analyte"], item["reason"], "rule", source=item.get("source"))

    # 3) По приросту информации на рубль — только когда есть что уточнять.
    if allow_information and case_model is not None and x_row is not None and len(out) < MAX_ITEMS:
        gains: dict[str, float] = {}
        for analyte, gain in case_model.information_gain(x_row).items():
            order = ORDER_AS.get(analyte, analyte)          # что реально заказывать в лаборатории
            if order in available or order in seen:
                continue
            gains[order] = max(gains.get(order, 0.0), float(gain))
        priced = {a: cfg.price(a) for a in gains}
        known = [p for p, ok in priced.values() if p and ok]
        med = median(known) if known else 1.0
        scored = []
        for analyte, gain in gains.items():
            price, is_available = priced[analyte]
            # Без цены не предлагаем: показатель либо не продаётся отдельно, либо входит в уже сданный анализ.
            if not is_available or not price or gain < MIN_GAIN_BITS:
                continue
            scored.append((gain / (1.0 + price / med), analyte, gain))
        rule_chosen = {t.analyte for t in out} | available
        picked: list[tuple[str, float]] = []
        limit = min(MAX_INFORMATION, MAX_ITEMS - len(out))
        for _score, analyte, gain in sorted(scored, reverse=True):
            if len(picked) >= limit:
                break
            chosen = rule_chosen | {a for a, _ in picked}
            if _redundant(analyte, chosen | {analyte}, rule_chosen):
                continue
            picked.append((analyte, gain))
            # НТЖ выбрано раньше, а теперь выбраны и железо, и ОЖСС: НТЖ из них считается — убираем его.
            if TSAT_PARTS <= rule_chosen | {a for a, _ in picked}:
                picked = [(a, g) for a, g in picked if a != "TSAT"]
        for analyte, gain in picked:
            add(analyte,
                f"Помогает различить оставшиеся варианты: прирост информации {_gain_text(gain)} бит "
                "(порядок в списке — с учётом цены).",
                "information", gain=round(float(gain), 3), source="расчёт по таблицам модели и ценам анализов")
    return out
