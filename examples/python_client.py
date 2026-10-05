#!/usr/bin/env python3
"""Клиент API DeficitLens на Python (библиотека requests).

Маршруты и форматы — по docs/CONTRACTS.md, раздел «Контракт HTTP»:
  GET  /healthz              — без ключа: состояние и версии
  POST /api/v1/analyze       — один анализ -> полный ответ (AnalysisResult)
  POST /api/v1/batch         — пакет записей -> сводка и результат по каждой записи
  POST /api/v1/benchmark     — файл CSV / XLSX в схеме кейса -> предсказания и сводка
  POST /api/v1/parse         — вставленный текст бланка -> значения для сверки
  GET  /api/v1/reference     — показатели, единицы, пороги с источниками, цены, версии
Ключ передаётся заголовком X-API-Key. Нет ключа — 401, неверный — 403, ошибка ввода — 422.

Установка:  pip install requests
Запуск примера:
    export DL_API_KEY=dl-demo-key                # ключ API
    export DL_BASE_URL=http://127.0.0.1:8080     # необязательно
    python3 examples/python_client.py

Сервис ничего не хранит. Не передавайте в запросах ФИО, даты рождения, номера полисов и другие персональные
данные: для расчёта нужны только пол, возраст в годах и значения анализов. Поле patient_ref — произвольная
метка строки (например, номер строки выгрузки); сервис возвращает её как есть.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

import requests


class DeficitLensError(Exception):
    """Ответ сервиса с ошибкой: код HTTP и тело {"error": {"code", "message", "field"}}."""

    def __init__(self, status: int, code: str, message: str, field: Optional[str] = None):
        super().__init__(f"HTTP {status} [{code}] {message}" + (f" (поле: {field})" if field else ""))
        self.status, self.code, self.message, self.field = status, code, message, field


class DeficitLensClient:
    """Клиент API. Один объект можно использовать для многих запросов."""

    def __init__(self, base_url: str = "http://127.0.0.1:8080", api_key: Optional[str] = None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-API-Key"] = api_key

    # --- служебное ---------------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        r = self.session.request(method, self.base_url + path, timeout=self.timeout, **kwargs)
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            raise DeficitLensError(r.status_code, err.get("code", "http_error"),
                                   err.get("message", r.text[:200]), err.get("field"))
        return r

    # --- маршруты ----------------------------------------------------------------------------
    def health(self) -> dict:
        """Состояние сервиса и версии движка, моделей и набора порогов. Ключ не нужен."""
        return self._request("GET", "/healthz").json()

    def analyze(self, sex: str, age_years: int, values: dict[str, Any], *, pregnancy: Optional[dict] = None,
                norms: str = "who", mode: str = "clinical", lab_reference: str = "default",
                patient_ref: Optional[str] = None) -> dict:
        """Один анализ.

        sex — "F" или "M"; age_years — от 18 лет; values — {код показателя: число} или
        {код: {"value": число, "unit": "единица"}}. Коды показателей — как столбцы файла кейса
        (hemoglobin, MCV, ferritin, vitamin_B12 …); полный список — в ответе reference().
        Без единицы значение считается записанным в канонической единице (гемоглобин — г/л, ферритин — мкг/л).
        norms — какой набор порогов показывать первым: "who" или "ru".
        mode — "clinical" (основной режим; ответ зависит только от сданных значений) или "benchmark".
        """
        body: dict[str, Any] = {"sex": sex, "age_years": age_years, "values": values, "norms": norms, "mode": mode,
                                "lab_reference": lab_reference}
        if pregnancy is not None:
            body["pregnancy"] = pregnancy
        if patient_ref is not None:
            body["patient_ref"] = patient_ref
        return self._request("POST", "/api/v1/analyze", json=body).json()

    def batch(self, records: list[dict], with_reports: bool = False) -> dict:
        """Пакет записей в формате analyze(). Ответ: {"summary": {...}, "items": [{index, patient_ref, status,
        errors, result}]}. Запись с ошибкой получает status = "rejected" и причину; остальные считаются.
        with_reports=False — без текстов отчётов врачу и пациенту (ответ заметно короче)."""
        return self._request("POST", "/api/v1/batch", json={"records": records, "with_reports": with_reports}).json()

    def benchmark(self, path: str | Path, mode: str = "auto", as_csv: bool = False) -> dict | str:
        """Файл CSV / XLSX в схеме кейса -> предсказания.

        mode: "auto", "clinical" или "benchmark". Режим "auto" выбирает ансамбль бенчмарка только для файлов
        от 30 строк, похожих на файл кейса; иначе работает клинический режим.
        as_csv=False -> dict {"summary", "preview" (первые 20 строк), "csv" (весь результат текстом)};
        as_csv=True  -> текст CSV.
        """
        p = Path(path)
        with open(p, "rb") as f:
            r = self._request("POST", "/api/v1/benchmark", params={"format": "csv"} if as_csv else None,
                              files={"file": (p.name, f)}, data={"mode": mode})
        return r.text if as_csv else r.json()

    def parse(self, text: str) -> dict:
        """Вставленный текст бланка -> {"values", "unrecognized", "notes"} для сверки глазами. Расшифровку не делает."""
        return self._request("POST", "/api/v1/parse", json={"text": text}).json()

    def reference(self) -> dict:
        """Справочник: показатели и единицы, пороги с источниками и статусами, цены, версии."""
        return self._request("GET", "/api/v1/reference").json()


# --------------------------------------------------------------------------------------------
# Пример использования. Все значения придуманы.
# --------------------------------------------------------------------------------------------
def main() -> int:
    base = os.environ.get("DL_BASE_URL", "http://127.0.0.1:8080")
    key = os.environ.get("DL_API_KEY")
    if not key:
        print("Задайте ключ API: export DL_API_KEY=<ключ>", file=sys.stderr)
        return 2
    client = DeficitLensClient(base, key)

    print("1. Состояние сервиса:", client.health())

    # 2. Без ключа сервис отказывает.
    try:
        DeficitLensClient(base).analyze("F", 34, {"hemoglobin": 124})
        print("2. ОШИБКА: запрос без ключа прошёл")
        return 1
    except DeficitLensError as e:
        print(f"2. Запрос без ключа отклонён: HTTP {e.status} — {e.message}")

    # 3. Один анализ: женщина 34 лет, гемоглобин в норме, ферритин не сдан.
    cbc = {"hemoglobin": 124, "RBC": 4.59, "hematocrit": 37.7, "MCV": 82, "MCH": 27.0, "MCHC": 329,
           "RDW": 15.2, "platelets": 310, "WBC": 6.1}
    res = client.analyze("F", 34, cbc, patient_ref="example-1")
    print("3. Анемия по ВОЗ:", res["level1"]["anemia"], "| порог, г/л:", res["level1"]["threshold_g_l"])
    hidden = res["hidden_deficiency"]
    screening = hidden.get("screening") or {}
    print("   Скрытый дефицит:", hidden["summary_ru"])
    print("   Скрининг: риск", screening.get("risk_ru"), "| P(ферритин < 15):", screening.get("p_ferritin_lt15"))
    for t in res["next_tests"]:
        print(f"   Досдать: {t['name_ru']} — {t['reason']}")
    print("   Врачу:", res["reports"]["doctor"]["headline"])
    print("   Пациенту:", res["reports"]["patient"]["headline"])

    # 4. Тот же анализ после досдачи ферритина; единица указана явно (нг/мл = мкг/л).
    res = client.analyze("F", 34, {**cbc, "ferritin": {"value": 9, "unit": "ng/mL"}})
    print("4. С ферритином 9:", [s["code"] for s in res["hidden_deficiency"]["rule_signals"]])

    # 5. Ошибка ввода: сервис рассчитан на взрослых.
    try:
        client.analyze("F", 12, {"hemoglobin": 118})
    except DeficitLensError as e:
        print(f"5. Ошибка ввода: HTTP {e.status} [{e.code}] {e.message}")

    # 6. Пакет: две записи, во второй нет гемоглобина — она будет отклонена, первая посчитана.
    out = client.batch([
        {"patient_ref": "row-1", "sex": "M", "age_years": 61,
         "values": {"hemoglobin": 104, "MCV": 79, "RDW": 16.0, "ferritin": 62, "CRP": 28}},
        {"patient_ref": "row-2", "sex": "F", "age_years": 40, "values": {"MCV": 88}},
    ])
    print("6. Пакет:", out["summary"])
    for item in out["items"]:
        if item["status"] == "done":
            r = item["result"]
            print(f"   {item['patient_ref']}: анемия {r['level1']['anemia']}, группа {r['level2']['case_group']}, "
                  f"флаги {[f['code'] for f in r['flags']]}")
        else:
            print(f"   {item['patient_ref']}: отклонена — {item['errors'][0]['message']}")

    # 7. Файл в схеме кейса (если путь передан аргументом).
    if len(sys.argv) > 1:
        b = client.benchmark(sys.argv[1], mode="auto")
        s = b["summary"]
        print(f"7. Файл: строк {s['rows_total']}, принято {s['rows_done']}, отклонено {s['rows_rejected']}, "
              f"режим {s['mode']}, время {s.get('seconds')} с")
        if (s.get("metrics") or {}).get("same_as_training_data"):
            print("   Внимание: файл совпадает с обучающим набором, метрики на нём завышены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
