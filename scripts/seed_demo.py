#!/usr/bin/env python3
"""Демо-данные для показа кабинета: пациент с тремя придуманными анализами и врач, которому открыт доступ.

Работает через API уже запущенного сервиса (ничего не пишет в базу напрямую). Все значения придуманы —
это не данные пациентов. Пароли печатаются в конце; без --password создаётся случайный.

Запуск:  python3 scripts/seed_demo.py [--base http://127.0.0.1:8080] [--prefix demo] [--password …]
Повторный запуск с тем же --prefix и --password входит в существующие аккаунты и добавляет недостающее.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request

# Три анализа одной пациентки (Ж, 34 года) за полгода: Hb и ферритин снижаются, MCV уменьшается, RDW растёт.
ANALYSES = [
    ("2026-03-12", {"hemoglobin": 132, "RBC": 4.6, "hematocrit": 40.1, "MCV": 88, "MCH": 29.0, "MCHC": 330,
                    "RDW": 13.1, "platelets": 260, "WBC": 6.0, "ferritin": 45, "CRP": 2}),
    ("2026-06-15", {"hemoglobin": 125, "RBC": 4.6, "hematocrit": 38.2, "MCV": 85, "MCH": 27.6, "MCHC": 326,
                    "RDW": 14.0, "platelets": 275, "WBC": 5.8, "ferritin": 28, "CRP": 1.5}),
    ("2026-09-20", {"hemoglobin": 118, "RBC": 4.5, "hematocrit": 36.4, "MCV": 81, "MCH": 26.2, "MCHC": 322,
                    "RDW": 15.6, "platelets": 290, "WBC": 6.2, "ferritin": 12, "CRP": 2}),
]
DOCTOR_REFS = {"hemoglobin": {"low": 117, "high": 155, "unit": "г/л"}, "MCV": {"low": 82, "high": 98, "unit": "фл"}}


def call(base: str, method: str, path: str, body=None, token: str | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def account(base: str, login: str, password: str, role: str, clinic: str | None = None) -> str:
    body = {"login": login, "password": password, "role": role, "consent": True}
    if clinic:
        body["clinic"] = clinic
    status, data = call(base, "POST", "/api/v1/auth/register", body)
    if status == 409:
        status, data = call(base, "POST", "/api/v1/auth/login", {"login": login, "password": password})
    if status not in (200, 201):
        sys.exit(f"{login}: {status} {data.get('error', {}).get('message', data)} — задайте другой --prefix или --password")
    return data["token"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--prefix", default="demo")
    ap.add_argument("--password", default=None, help="общий пароль демо-аккаунтов (по умолчанию случайный)")
    a = ap.parse_args()
    base, password = a.base.rstrip("/"), a.password or ("Demo-" + secrets.token_urlsafe(9))
    patient, doctor = f"{a.prefix}.patient", f"{a.prefix}.doctor"

    pt = account(base, patient, password, "patient")
    have = {r["date"] for r in call(base, "GET", "/api/v1/me/records", token=pt)[1].get("records", [])}
    for date, values in ANALYSES:
        if date in have:
            continue
        status, data = call(base, "POST", "/api/v1/me/records", {
            "date": date, "source": "form", "input": {"sex": "F", "age_years": 34, "values": values}}, token=pt)
        if status != 201:
            sys.exit(f"запись {date}: {status} {data}")

    dt = account(base, doctor, password, "doctor", clinic="Демо-клиника")
    sets = call(base, "GET", "/api/v1/doctor/lab-refs", token=dt)[1].get("sets", [])
    if not sets:
        call(base, "POST", "/api/v1/doctor/lab-refs", {"name": "Лаборатория демо", "ranges": DOCTOR_REFS,
                                                       "default": True}, token=dt)
    patients = call(base, "GET", "/api/v1/doctor/patients", token=dt)[1].get("patients", [])
    if not any(p.get("login") == patient for p in patients):
        status, share = call(base, "POST", "/api/v1/me/shares", {"days": 30, "password": password}, token=pt)
        if status != 200:
            sys.exit(f"ссылка врачу: {status} {share}")
        status, acc = call(base, "POST", "/api/v1/shares/accept", {"token": share["link_token"]}, token=dt)
        if status != 200:
            sys.exit(f"принятие ссылки: {status} {acc}")

    hist = call(base, "GET", "/api/v1/me/history", token=pt)[1]
    print(f"Готово: {base}/login")
    print(f"  пациент: {patient}  пароль: {password}  (анализов: {hist.get('n_records')}, событий: {len(hist.get('events', []))})")
    print(f"  врач:    {doctor}  пароль: {password}  (доступ к {patient} на 30 дней, референсы «Лаборатория демо»)")
    print("Все значения придуманы. Удалить: в кабинете «Мои данные» → «Удалить всё».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
