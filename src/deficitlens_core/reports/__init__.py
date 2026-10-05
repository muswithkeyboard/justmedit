"""Отчёты врачу и пациенту.

Тексты-шаблоны лежат в config/texts_ru/doctor.yaml и patient.yaml; здесь и в doctor.py / patient.py — только сборка.
Общие правила:
  - врачу — дообследование по правилам и перечню анализов; лечение — РОВНО одной строкой-ссылкой на клинические
    рекомендации Минздрава России, без схем и доз;
  - пациенту — без чисел вероятности, без диагнозов как утверждений, без лекарств, доз, добавок и диет;
    каждая строка проходит фильтр config/forbidden_patient_words.yaml: строка с запрещённой подстрокой
    заменяется нейтральной «Обсудите этот результат с врачом.»;
  - оба отчёта заканчиваются строкой «Исследовательский прототип, не медицинское изделие. Решение принимает врач.»
"""
from __future__ import annotations

from typing import Any, Iterable

from ..config import Config
from ..schemas import (ChecklistItem, Explanation, Flag, HiddenDeficiency, Level1, Level2, NextTest, Report, Reports,
                       ReportSection, ValueRow)

# Обязательные строки. Основной источник — config/texts_ru/*.yaml; эти значения — страховка,
# если файла текстов нет: без этих строк отчёт выходить не должен.
DISCLAIMER = "Исследовательский прототип, не медицинское изделие. Решение принимает врач."
TREATMENT_LINE = ("Лечение — по клиническим рекомендациям Минздрава России (ID 669, 536, 540); "
                  "схемы и дозы прототип не приводит.")
NEUTRAL_PATIENT_LINE = "Обсудите этот результат с врачом."


# --------------------------------------------------------------------------------------------
# Доступ к шаблонам
# --------------------------------------------------------------------------------------------
class _Safe(dict):
    """Подстановки шаблона: неизвестный ключ остаётся в тексте как есть, а не роняет сборку."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Texts:
    """Шаблоны одного отчёта (словарь из config/texts_ru/<имя>.yaml). Нет шаблона — пустая строка."""

    def __init__(self, data: Any):
        self._data = data if isinstance(data, dict) else {}

    def raw(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def t(self, path: str, **kw: Any) -> str:
        template = self.raw(path)
        if not isinstance(template, str):
            return ""
        try:
            return template.format_map(_Safe(kw))
        except (ValueError, IndexError, KeyError, AttributeError):
            return template


class Lines(list):
    """Список строк раздела: пустые строки не добавляются, лишние пробелы убираются."""

    def add(self, text: Any) -> None:
        text = " ".join(str(text or "").split())
        if text:
            self.append(text)


# --------------------------------------------------------------------------------------------
# Мелкие помощники текста
# --------------------------------------------------------------------------------------------
def cap(text: str) -> str:
    """Первая буква — заглавная."""
    return text[:1].upper() + text[1:] if text else text


def decap(text: str) -> str:
    """Первая буква — строчная, если это обычное слово (сокращения вроде «B12», «ОЖСС», «MCV» не трогаем)."""
    if len(text) >= 2 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def sentence(text: str) -> str:
    """Текст с точкой в конце (если её нет)."""
    text = (text or "").strip()
    if text and text[-1] not in ".!?…":
        text += "."
    return text


def pct(p: float | None) -> str:
    """Вероятность -> целое число процентов для отчёта врачу («62», «<1»)."""
    if p is None:
        return "—"
    value = 100.0 * float(p)
    if 0 < value < 1:
        return "<1"
    return str(int(round(value)))


# --------------------------------------------------------------------------------------------
# Фильтр текста пациента
# --------------------------------------------------------------------------------------------
def _norm_for_filter(text: Any) -> str:
    return str(text).lower().replace("ё", "е")


def filter_patient_line(line: Any, forbidden: Iterable[str], neutral: str = NEUTRAL_PATIENT_LINE) -> str:
    """Строка пациента или нейтральная строка, если в ней есть запрещённая подстрока. Не падает ни на каком входе."""
    try:
        low = _norm_for_filter(line)
        for word in forbidden or ():
            if word is None:
                continue
            w = _norm_for_filter(word)
            if w and w in low:
                return neutral
        return str(line)
    except Exception:  # noqa: BLE001 — фильтр не должен ронять отчёт
        return neutral


def filter_patient_lines(lines: Iterable[Any], forbidden: Iterable[str],
                         neutral: str = NEUTRAL_PATIENT_LINE) -> list[str]:
    """Фильтр по всем строкам раздела; подряд идущие нейтральные строки схлопываются в одну."""
    forbidden = list(forbidden or ())
    out: list[str] = []
    for line in lines:
        safe = filter_patient_line(line, forbidden, neutral)
        if safe == neutral and out and out[-1] == neutral:
            continue
        out.append(safe)
    return out


# --------------------------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------------------------
def build_reports(*, norm, level1: Level1, level2: Level2, hidden: HiddenDeficiency, flags: list[Flag],
                  checklist: list[ChecklistItem], next_tests: list[NextTest], explanation: Explanation,
                  values: list[ValueRow], cfg: Config) -> Reports:
    """Два отчёта по одному разбору: врачу и пациенту."""
    from .doctor import build_doctor_report
    from .patient import build_patient_report

    kwargs = dict(norm=norm, level1=level1, level2=level2, hidden=hidden, flags=flags or [], checklist=checklist or [],
                  next_tests=next_tests or [], explanation=explanation or Explanation(), values=values or [], cfg=cfg)
    return Reports(doctor=build_doctor_report(**kwargs), patient=build_patient_report(**kwargs))


__all__ = ["build_reports", "filter_patient_line", "filter_patient_lines", "Texts", "Lines",
           "DISCLAIMER", "TREATMENT_LINE", "NEUTRAL_PATIENT_LINE", "Report", "ReportSection"]
