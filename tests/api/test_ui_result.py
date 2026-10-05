"""Страница результата (поток П1, 04.10 вечер): справочник для карточек 20/30/40 %, разметка главной.

Числа карточек «кому предложить ферритин» берутся только из /ui/reference → screening.referral, а туда — из
docs/metrics/screen_metrics.json → summary.referral_F18-49_t15 (в коде страницы чисел нет).
"""
from __future__ import annotations

import json
import re

import helpers

from deficitlens_api import service

TEMPLATES = helpers.ROOT / "src" / "deficitlens_api" / "templates"
STATIC = helpers.ROOT / "src" / "deficitlens_api" / "static"


def _ui_reference() -> dict:
    """/ui/reference через приложение (TestClient), без httpx — тот же словарь из service.reference()."""
    try:
        from starlette.testclient import TestClient

        from deficitlens_api.app import create_app
        from deficitlens_api.settings import Settings
        client = TestClient(create_app(Settings(api_keys=("test-key-1",), ui_rate_per_min=10000)))
    except Exception:  # noqa: BLE001 — нет httpx
        return service.reference()
    r = client.get("/ui/reference")
    assert r.status_code == 200
    return r.json()


def test_reference_screening_referral_from_metrics():
    scr = _ui_reference()["screening"]
    assert scr["refer_share"] == 0.3 and scr["refer_share_options"] == [0.2, 0.3, 0.4]
    path = service.metrics_dir() / "screen_metrics.json"
    if not path.exists():
        assert scr["referral"] == [] and scr["referral_meta"] is None
        return
    src = {round(r["refer_share"], 2): r for r in json.loads(path.read_text(encoding="utf-8"))["summary"]["referral_F18-49_t15"]}
    rows = scr["referral"]
    assert [r["refer_share"] for r in rows] == [0.2, 0.3, 0.4]                        # только варианты переключателя
    for r in rows:
        assert set(r) == {"refer_share", "referred_per1000", "found_per1000", "cases_per1000", "sensitivity", "ppv"}
        for k in ("referred_per1000", "found_per1000", "cases_per1000", "sensitivity", "ppv"):
            assert r[k] == src[r["refer_share"]][k], (r["refer_share"], k)
        assert r["found_per1000"] <= r["cases_per1000"] <= 1000 and 0 < r["ppv"] < 1
    meta = scr["referral_meta"]
    assert meta["ferritin_below"] == 15 and "18–49" in meta["group_ru"] and meta["validation"]


def test_reference_screening_referral_without_metrics_file():
    import os
    import tempfile
    old = os.environ.get("DL_METRICS_DIR")
    with tempfile.TemporaryDirectory() as d:
        os.environ["DL_METRICS_DIR"] = d                     # файла метрик нет — карточки без чисел, без ошибки
        try:
            assert service.screen_referral() == {"referral": [], "referral_meta": None}
        finally:
            if old is None:
                os.environ.pop("DL_METRICS_DIR", None)
            else:
                os.environ["DL_METRICS_DIR"] = old


def test_page_markup_result_and_pdf():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    # «Загрузить PDF»: своё поле файла только для PDF, две кнопки (полоса — <button>, зона загрузки — <label>)
    assert re.search(r'<input id="paste-pdf"[^>]*type="file"[^>]*accept="\.pdf,application/pdf"', html)
    assert html.count('for="paste-pdf"') == 1 and 'data-file="paste-pdf"' in html and "ЕМИАС, Госуслуг или лаборатории" in html
    # строка-сводка результата и свёрнутое «Где сдать рядом»
    for id_ in ("result-sum-text", "edit-data", "new-analysis", "nearby-sum", "nearby-loading"):
        assert f'id="{id_}"' in html, id_
    assert re.search(r'<details class="card nearby" id="nearby"', html)
    # карточки 20/30/40 % без чисел в разметке: числа приходят из справочника
    cards = re.findall(r'name="refer_card" value="(0\.\d)"', html)
    assert cards == ["0.2", "0.3", "0.4"]
    assert not re.search(r"\b(48|56|63) из 72\b", html)


def test_page_script_takes_numbers_from_reference():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "scr.referral" in js and "referral_meta" in js
    assert not re.search(r"\b(200|300|400)\s*из\s*1000\b", js)                      # метрики не захардкожены
    assert "reference_ranges" in js and "analysisDate" in js                         # поля ответа /parse (контракт П2)
    assert "innerHTML" not in js


def test_docx_export_has_footer_and_brand():
    js = (STATIC / "export.js").read_text(encoding="utf-8")
    assert "word/footer1.xml" in js and 'r:id="rId2"' in js and "relationships/footer" in js
    assert "PAGE" in js and "NUMPAGES" in js
    assert '"Justmed"' in js and 'run("it"' in js and "061019" in js


def test_hero_upload_buttons_are_buttons_and_photo_not_camera_only():
    """UX-ревизия 05.10: главные кнопки полосы — <button> (Tab их не пропускает), основная кнопка фото без capture
    (телефон предложит камеру или галерею), выбор файла — без системного «Choose File»."""
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    hero = html[html.index('class="hero__cta"'):html.index('id="hero-manual"')]
    assert hero.count('<button type="button" class="btn btn--primary btn--hero"') == 2 and "<label" not in hero
    assert 'data-file="paste-photo"' in hero and 'data-file="paste-pdf"' in hero
    assert "capture=" not in html
    assert re.search(r'<input id="paste-file" class="vh"[^>]*type="file"', html) and 'for="paste-file" class="btn"' in html
    assert 'id="paste-file-run"' not in html
    # фокус после расчёта — на сводке результата; вывод объявляется живой областью
    assert re.search(r'id="result-sum" tabindex="-1"', html)
    assert re.search(r'<p id="result-live" class="vh" role="status" aria-live="polite">', html)
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '$("result-sum").focus({ preventScroll: true })' in js
    assert '$($(id).getAttribute("data-file")).click()' in js


def test_page_texts_match_server_and_units_come_from_reference():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert service._FIELD_MESSAGES["age_years"] in js                     # ошибка возраста — как у сервера
    assert "например 12,5" not in js                                       # Hb «12,5» — это г/дл, не пример
    assert "unit_choices" in js and "чувствительность " in js
    ref = _ui_reference()
    by = {a["code"]: a for a in ref["analytes"]}
    hb = by["hemoglobin"]["unit_choices"]
    assert hb[0]["factor"] == 1.0 and {"unit": "г/дл", "factor": 10.0} in hb   # каноническая первой, русское название
    assert [u["factor"] for u in by["MCV"]["unit_choices"]] == [1.0]       # одна единица — списка выбора нет
    for a in ref["analytes"]:
        factors = [u["factor"] for u in a["unit_choices"]]
        assert len(factors) == len(set(factors)) and factors[0] == 1.0, a["code"]


def test_css_focus_ring_small_text_and_phone_inputs():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    tail = css[css.index("UX-ревизия главной"):]
    assert ":focus-visible { outline: 3px solid var(--sea)" in tail                 # не мятное на белом
    assert ".top :focus-visible, .notice :focus-visible, .hero :focus-visible, .foot :focus-visible { outline-color: var(--mint); }" in tail
    assert ".vh { position: absolute !important; width: 1px !important;" in tail      # скрытый input не растягивает страницу
    assert "font-size: 16px !important" in tail                                      # iOS не увеличивает страницу
    # ни одной подписи мельче .7rem после ревизии (кроме служебных пометок кабинета в шапке)
    for size in re.findall(r"font-size:\s*\.(\d+)rem", tail):
        assert int(size.ljust(2, "0")[:2]) >= 70, size


def test_benchmark_page_translates_preview_and_rejects_empty_file():
    js = (STATIC / "benchmark.js").read_text(encoding="utf-8")
    assert "PREVIEW_HEAD" in js and "labels.classes12" in js and "labels.causes11" in js
    assert "В файле нет строк с данными" in js and "unit_choices" in js
    assert "innerHTML" not in js
