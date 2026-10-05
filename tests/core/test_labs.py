"""Блок «Где сдать рядом» (ADR 0008): файл пунктов сетей лабораторий и правила приватности на странице.

Файл static/labs/labs_msk.json собран scripts/build_labs.py из OpenStreetMap (ODbL). Проверяем формат, границы региона,
отсутствие посторонних объектов, ссылки только http(s); на странице — что координаты пользователя не уходят в ссылки
и запросы, а подложка карты — единственный внешний адрес.
"""
from __future__ import annotations

import json
import re
import sys

import helpers

STATIC = helpers.ROOT / "src" / "deficitlens_api" / "static"
sys.path.insert(0, str(helpers.ROOT / "scripts"))


def _labs() -> dict:
    return json.loads((STATIC / "labs" / "labs_msk.json").read_text(encoding="utf-8"))


def test_labs_file_format_and_region():
    d = _labs()
    assert "OpenStreetMap" in d["source"] and "ODbL" in d["source"] and d["osm_base"]
    assert d["fields"] == ["lat", "lon", "name", "network", "address", "opening_hours", "website"]
    labs = d["labs"]
    assert len(labs) >= 500
    for lat, lon, name, net, addr, hours, site in labs:
        assert 54.2 <= lat <= 56.97 and 35.1 <= lon <= 40.25, (lat, lon, name)      # Москва и Московская область
        assert name and net, name                                                   # только пункты сетей лабораторий
        assert not site or re.match(r"^https?://[^\s\"'<>]+$", site), site
        assert "политех" not in name.lower() and "колледж" not in name.lower(), name
    assert len({(round(x[0], 4), round(x[1], 4), x[3]) for x in labs}) == len(labs)  # без дублей


def test_build_labs_filter():
    import build_labs as b
    assert b.network({"name": "Инвитро"}) == "Инвитро"
    assert b.network({"name": "Московский Политех"}) is None                        # «Литех» внутри слова — не сеть
    assert b.keep({"healthcare": "laboratory", "name": "Гемотест"})
    assert not b.keep({"healthcare": "laboratory", "name": "Центр технических измерений"})   # не сеть лабораторий
    assert not b.keep({"amenity": "college", "name": "Литех колледж"})
    assert not b.keep({"healthcare": "dentist", "name": "CMD"})
    out = b.build({"elements": [
        {"type": "node", "lat": 55.75, "lon": 37.6, "tags": {"healthcare": "laboratory", "name": "Инвитро",
                                                            "website": "javascript:alert(1)"}},
        {"type": "way", "center": {"lat": 55.7, "lon": 37.5}, "tags": {"amenity": "doctors", "brand": "KDL", "name": "KDL"}},
    ]})
    assert [x[2] for x in out["labs"]] == ["KDL", "Инвитро"] and out["labs"][1][6] == ""   # опасная ссылка отброшена


def test_page_keeps_user_location_local():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    # единственный внешний адрес загрузки — подложка карты (OSM; CARTO — с поддоменами a–d, если будет ключ API);
    # поиск и маршрут — ссылки по нажатию, без координат пользователя
    loads = {re.sub(r"^https://\{s\}\.", "https://", u) for u in re.findall(r"https://[a-z0-9.{}-]+", js)}
    assert loads <= {"https://tile.openstreetmap.org", "https://basemaps.cartocdn.com", "https://yandex.ru"}, loads
    assert len(re.findall(r'\bTILES = "https://', js)) == 1
    assert "attributionControl: false" in js and "attribution:" not in js    # надписи в углу карты убраны
    route = re.search(r"function yandexRoute\(lat, lon\) \{ return ([^}]*)\}", js).group(1)
    assert "rtext=~" in route                                       # точка старта пустая: положение определят сами карты
    search = re.search(r"function yandexSearch\(name\) \{ return ([^}]*)\}", js).group(1)
    assert "near.point" not in search and "ll=" not in search
    assert "fetch(" not in js.split("// ---------- где сдать рядом")[1].split("// ---------- выгрузка")[0]
    assert "referrerPolicy: \"strict-origin\"" in js               # подложке — только origin, без пути страницы
    # авторство (ODbL; для CARTO — и его условия) — строкой под картой, ссылки без Referer
    html = (helpers.ROOT / "src" / "deficitlens_api" / "templates" / "index.html").read_text(encoding="utf-8")
    credit = re.search(r'<p class="nearby__credit"[^>]*>(.*?)</p>', html, flags=re.DOTALL).group(1)
    assert "OpenStreetMap" in credit and "(ODbL)" in credit
    assert ("cartocdn" in js) == ("CARTO" in credit)
    links = re.findall(r"<a\b[^>]*>", credit)
    assert links and all('rel="noopener noreferrer"' in a for a in links), links


def test_vendored_leaflet_is_pinned():
    import hashlib
    import base64
    js = (STATIC / "vendor" / "leaflet" / "leaflet.js").read_bytes()
    css = (STATIC / "vendor" / "leaflet" / "leaflet.css").read_bytes()
    sri = lambda b: base64.b64encode(hashlib.sha256(b).digest()).decode()   # noqa: E731
    # хэши опубликованы на leafletjs.com/download.html для версии 1.9.4
    assert sri(js) == "20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    assert sri(css) == "p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    assert (STATIC / "vendor" / "leaflet" / "LICENSE").read_text().startswith("BSD 2-Clause License")
