"""Своя структура файла на странице «Проверить на своём файле»: /ui/columns (что сервис понял в каждом столбце,
догадки для своих названий, проверка «хватает ли данных») и поле mapping у расчёта (сопоставление, «не использовать»,
пересчёт единиц). Плюс регион цен в справочнике страницы. Все значения придуманы."""
from __future__ import annotations

import io
import json

from deficitlens_api.app import create_app
from deficitlens_api.settings import Settings

KEY = "columns-key-1"
HEADERS = {"X-API-Key": KEY}
# свои названия столбцов, «;», десятичная запятая, гемоглобин в г/дл, лишний столбец
CUSTOM = ("Пациент;Пол;Возраст;HGB, г/дл;Ферритин;Комментарий\n"
          "1;Ж;34;11,2;9;—\n2;М;55;14,5;80;повтор\n3;ж;29;12,8;40;\n")


def _client():
    from starlette.testclient import TestClient
    return TestClient(create_app(Settings(api_keys=(KEY,), ui_rate_per_min=10000)))


def _file(text: str, name: str = "my.csv"):
    return {"file": (name, io.BytesIO(text.encode("utf-8")), "text/csv")}


def test_columns_preview_recognizes_guesses_and_requirements():
    r = _client().post("/ui/columns", files=_file(CUSTOM))
    assert r.status_code == 200, r.text
    d = r.json()
    cols = {c["column"]: c for c in d["columns"]}
    assert d["rows"] == 3
    assert cols["Пол"]["kind"] == "feature" and cols["Пол"]["target"] == "sex"
    assert cols["Ферритин"]["target"] == "ferritin"
    assert cols["HGB, г/дл"]["kind"] == "guess" and cols["HGB, г/дл"]["target"] == "hemoglobin"
    assert cols["HGB, г/дл"]["unit"] == "g/dl"                      # единица из заголовка — догадка для выбора
    assert cols["Пациент"]["target"] == "patient_id" and cols["Комментарий"]["kind"] == "unknown"
    req = d["requirements"]                                         # догадка ещё не принята — гемоглобина нет
    assert not req["ok"] and any("гемоглобин" in e for e in req["errors"])
    assert any("общего анализа крови" in w for w in req["warnings"])
    assert any("метками" in i for i in req["info"])
    hb = next(t for t in d["targets"] if t["code"] == "hemoglobin")
    assert {"unit": "g/dl", "factor": 10.0} in hb["units"]
    # тот же маршрут для интеграции — только с ключом
    c = _client()
    assert c.post("/api/v1/columns", files=_file(CUSTOM)).status_code == 401
    assert c.post("/api/v1/columns", files=_file(CUSTOM), headers=HEADERS).status_code == 200


def test_benchmark_with_mapping_units_and_ignore():
    mapping = {"HGB, г/дл": {"code": "hemoglobin", "unit": "г/дл"}, "Комментарий": "ignore", "Пациент": "patient_id"}
    r = _client().post("/ui/benchmark", files=_file(CUSTOM), data={"mode": "clinical", "mapping": json.dumps(mapping)})
    assert r.status_code == 200, r.text
    d = r.json()
    s = d["summary"]
    assert s["rows_done"] == 3 and s["rows_rejected"] == 0
    rows = {p["patient_id"]: p for p in d["preview"]}
    assert rows["1"]["anemia"] == 1 and rows["2"]["anemia"] == 0          # 11,2 г/дл = 112 г/л < 120 (Ж); 145 ≥ 130 (М)
    assert s["requirements"]["ok"] and not s["requirements"]["errors"]
    assert "Комментарий" not in json.dumps(s.get("columns", {}), ensure_ascii=False)
    # без сопоставления гемоглобина нет — каждая строка отклонена, сводка говорит почему
    bare = _client().post("/ui/benchmark", files=_file(CUSTOM), data={"mode": "clinical"}).json()["summary"]
    assert bare["rows_done"] == 0 and not bare["requirements"]["ok"]


def test_mapping_errors_are_422():
    c = _client()
    for mapping, word in (('{"Нет такого": "hemoglobin"}', "нет в файле"), ('{"Пол": "nonsense"}', "неизвестный"),
                          ('{"HGB, г/дл": {"code": "hemoglobin", "unit": "кг"}}', "единица"), ("[1, 2]", "объект"),
                          ("не json", "JSON")):
        r = c.post("/ui/benchmark", files=_file(CUSTOM), data={"mapping": mapping})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_mapping", (mapping, r.text)
        assert word in r.json()["error"]["message"] and r.json()["error"]["field"] == "mapping", mapping


def test_reference_has_price_region():
    ref = _client().get("/ui/reference").json()
    reg = ref["prices"]["regions"][0]
    assert reg["code"] == "msk" and reg["name_ru"] == "Москва и Московская область" and len(reg["bbox"]) == 4
    assert "медиана" in reg["note"] and ref["prices"]["items"]["ferritin"]["price"] == 845
