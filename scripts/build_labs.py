#!/usr/bin/env python3
"""Собирает список пунктов сдачи анализов для блока «Где сдать рядом»: static/labs/labs_msk.json.

Источник — OpenStreetMap (© участники OpenStreetMap, лицензия ODbL 1.0), выгрузка через Overpass API.
Выгрузка делается вручную, один раз, на машине разработчика; работающий сервис наружу не ходит, а браузер
считает ближайшие пункты сам по этому файлу — координаты пользователя никуда не отправляются (ADR 0008).

Что попадает в список (Москва и Московская область): только пункты известных сетей лабораторий (NETWORKS ниже), где
сдают анализы из блока «Что досдать», — объекты healthcare=laboratory|sample_collection, клиники и кабинеты
(healthcare=clinic|doctor, amenity=clinic|doctors) и объекты без типа, если в названии, бренде или операторе — сеть.
Лаборатории без сети (исследовательские, санитарные, генетические центры) и учебные заведения, общежития и прочее
с похожими названиями («Политех» ≠ «Литех») отбрасываются.

Запуск:
  python3 scripts/build_labs.py --fetch            # скачать из Overpass и собрать файл
  python3 scripts/build_labs.py --raw выгрузка.json # собрать из уже скачанной выгрузки Overpass (JSON)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "deficitlens_api" / "static" / "labs" / "labs_msk.json"
OVERPASS = "https://overpass-api.de/api/interpreter"

# Сети лабораторий: шаблон названия -> как показывать. Граница слова слева — чтобы «Политех» не стал «Литех».
NETWORKS = [
    (r"инвитро|invitro", "Инвитро"), (r"гемотест|gemotest", "Гемотест"), (r"kdl|кдл", "KDL"),
    (r"cmd|цмд", "CMD"), (r"ситилаб|citilab", "Ситилаб"), (r"лабквест|labquest", "ЛабКвест"),
    (r"хеликс|helix", "Хеликс"), (r"днком|dnkom", "ДНКОМ"), (r"литех", "Литех"), (r"lab4u", "Lab4U"),
    (r"диалаб", "Диалаб"), (r"пикассо", "Пикассо"), (r"горлаб", "Горлаб"), (r"novascreen", "NovaScreen"),
    (r"labnet", "Labnet"), (r"chromolab|хромолаб", "Chromolab"),
]
NET_RE = [(re.compile(r"(?<![a-zа-яё])(?:" + p + r")", re.IGNORECASE), name) for p, name in NETWORKS]

# Запрос, которым сделана выгрузка 04.10.2026 (OSM на 09:55 UTC). Широкий (по brand/name без привязки к типу
# объекта) — лишнее отсекает keep(); при перегрузке Overpass запрос может не уложиться в таймаут — повторить позже.
QUERY = """[out:json][timeout:120];
area["ISO3166-2"~"^RU-(MOW|MOS)$"]->.a;
(
  nwr["healthcare"="laboratory"](area.a);
  nwr["healthcare"="sample_collection"](area.a);
  nwr["brand"~"Инвитро|INVITRO|Гемотест|KDL|КДЛ|CMD|ЦМД|Хеликс|Ситилаб|ЛабКвест|Lab4U|ДНКОМ|Диалаб|Литех",i](area.a);
  nwr["name"~"Инвитро|INVITRO|Гемотест|KDL|КДЛ|CMD|ЦМД|Хеликс|Ситилаб|ЛабКвест|Lab4U|ДНКОМ|Диалаб|Литех",i]["amenity"!~"pharmacy"]["shop"!~"."](area.a);
);
out center tags;
"""
SKIP_AMENITY = {"college", "university", "school", "dormitory", "kindergarten", "pharmacy", "hookah_lounge"}


def network(tags: dict) -> str | None:
    text = " ".join(tags.get(k, "") for k in ("brand", "name", "operator"))
    for rx, name in NET_RE:
        if rx.search(text):
            return name
    return None


def keep(tags: dict) -> bool:
    if tags.get("amenity") in SKIP_AMENITY or tags.get("shop") or network(tags) is None:
        return False
    hc, amenity = tags.get("healthcare", ""), tags.get("amenity", "")
    if hc in ("laboratory", "sample_collection", "clinic", "doctor") or amenity in ("clinic", "doctors"):
        return True
    return not hc and not amenity and not tags.get("office") and not tags.get("building")


def address(tags: dict) -> str:
    street, house = tags.get("addr:street", ""), tags.get("addr:housenumber", "")
    city = tags.get("addr:city", "")
    parts = [p for p in (city if city and city != "Москва" else "", street, house) if p]
    return ", ".join(parts) or tags.get("addr:full", "")


def website(tags: dict) -> str:
    url = tags.get("website") or tags.get("contact:website") or ""
    return url if re.match(r"^https?://[^\s\"'<>]+$", url) else ""


def build(raw: dict) -> dict:
    labs, seen = [], set()
    for e in raw.get("elements", []):
        tags = e.get("tags") or {}
        lat = e.get("lat", (e.get("center") or {}).get("lat"))
        lon = e.get("lon", (e.get("center") or {}).get("lon"))
        if lat is None or lon is None or not keep(tags):
            continue
        net = network(tags)
        name = (tags.get("name") or net or "Лаборатория").strip()[:80]
        lat, lon = round(lat, 5), round(lon, 5)
        key = (round(lat, 4), round(lon, 4), net or name)     # один пункт, отмеченный и точкой, и зданием
        if key in seen:
            continue
        seen.add(key)
        labs.append([lat, lon, name, net or "", address(tags)[:120],
                     tags.get("opening_hours", "")[:120], website(tags)[:200]])
    labs.sort(key=lambda x: (x[0], x[1]))
    return {
        "source": "© участники OpenStreetMap, ODbL 1.0 — https://www.openstreetmap.org/copyright",
        "osm_base": (raw.get("osm3s") or {}).get("timestamp_osm_base", ""),
        "region": "Москва и Московская область",
        "fields": ["lat", "lon", "name", "network", "address", "opening_hours", "website"],
        "labs": labs,
    }


def fetch(tries: int = 4) -> dict:
    """Выгрузка из Overpass; сервер бывает перегружен (504, «remark» с ошибкой) — несколько попыток с паузой."""
    data = urllib.parse.urlencode({"data": QUERY}).encode()
    last = ""
    for i in range(tries):
        req = urllib.request.Request(OVERPASS, data=data, headers={
            "User-Agent": "justmedit-hackathon/0.1 (one-time export)", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = json.load(resp)
            if raw.get("elements") and not raw.get("remark"):
                return raw
            last = raw.get("remark") or "пустой ответ"
        except (OSError, ValueError) as exc:          # HTTPError/URLError/таймаут — подклассы OSError
            last = str(exc)
        print(f"Overpass: попытка {i + 1} не удалась ({last}); жду {20 * (i + 1)} с", file=sys.stderr)
        time.sleep(20 * (i + 1))
    raise SystemExit(f"Overpass недоступен: {last}. Файл не изменён.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fetch", action="store_true", help="скачать выгрузку из Overpass API")
    g.add_argument("--raw", type=Path, help="готовая выгрузка Overpass (JSON)")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    raw = fetch() if a.fetch else json.loads(a.raw.read_text(encoding="utf-8"))
    out = build(raw)
    if len(out["labs"]) < 100:                         # защита от неполной выгрузки: старый файл не затираем
        raise SystemExit(f"В выгрузке только {len(out['labs'])} пунктов — похоже на сбой. Файл не изменён.")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    nets = {}
    for lab in out["labs"]:
        nets[lab[3] or "—"] = nets.get(lab[3] or "—", 0) + 1
    print(f"{a.out.relative_to(ROOT) if a.out.is_relative_to(ROOT) else a.out}: {len(out['labs'])} пунктов, "
          f"OSM на {out['osm_base']}; по сетям: " + ", ".join(f"{k} {v}" for k, v in sorted(nets.items(), key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
