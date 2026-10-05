"""Лёгкие проверки scripts/make_figures.py и docs/pitch/numbers.json (без построения рисунков и без данных)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
NUMBERS = ROOT / "docs" / "pitch" / "numbers.json"
FIGURES_DIR = ROOT / "docs" / "pitch" / "figures"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_make_figures_lists_eight_figures() -> None:
    mf = _load("make_figures")
    assert set(mf.FIGURES) == {"accuracy_by_level", "confusion5_benchmark", "confusion5_clinical", "referral_curve",
                               "calibration_raw_vs_z", "rdw_shift", "hidden_deficiency_by_sex_age",
                               "stfr_inflammation"}
    assert mf.main(["--list"]) == 0


def test_build_numbers_without_nhanes_uses_metrics_only() -> None:
    mf = _load("make_figures")
    saved = os.environ.pop("DL_NHANES_DATA", None)
    try:
        data = mf.build_numbers(mf.load_ctx())
    finally:
        if saved is not None:
            os.environ["DL_NHANES_DATA"] = saved
    items = data["items"]
    assert all(v["source"] and "value" in v for v in items.values())
    assert abs(items["case.benchmark.extended.acc12"]["value"] - 0.919) < 0.002
    assert items["screen.cohort.test_normal_hb"]["value"] == 5728  # правка 12: не 6 461
    assert round(items["case.naive.extended.acc5"]["value"], 2) == 0.94  # правка 12: типовой бустинг по 5 группам
    main = items["screen.auc.F18_49.t15"]
    assert main["n"] == 2180 and len(main["ci95"]) == 2 and main["ci95"][0] < main["value"] < main["ci95"][1]
    assert not any(k.startswith(("screen.stfr.", "screen.hidden.")) for k in items)  # без файла и без прежних чисел


def test_numbers_json_valid_and_matches_metrics() -> None:
    data = json.loads(NUMBERS.read_text(encoding="utf-8"))
    items = data["items"]
    assert len(items) > 200
    for k, v in items.items():
        assert set(v) >= {"value", "source", "label_ru"}, k
    metrics = json.loads((ROOT / "docs" / "metrics" / "screen_metrics.json").read_text(encoding="utf-8"))
    assert items["screen.auc.F18_49.t15"]["value"] == metrics["summary"]["by_group"]["F18-49"]["t15"]["auc"]
    rows = data["comparison_with_NUMBERS_md"]
    assert rows and all("key" in r and "over_0_02" in r for r in rows)


def test_no_word_synthetic_in_pitch_outputs() -> None:
    # правка 3: про файл кейса — «учебный набор кейса (840 строк)», не «синтетический»
    texts = [NUMBERS.read_text(encoding="utf-8"), (ROOT / "scripts" / "make_figures.py").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in FIGURES_DIR.glob("*.svg")]
    assert not any("синтетич" in t.lower() for t in texts)


def test_figures_exist_png_and_svg() -> None:
    mf = _load("make_figures")
    for name in mf.FIGURES:
        for ext in ("png", "svg"):
            p = FIGURES_DIR / f"{name}.{ext}"
            assert p.is_file() and p.stat().st_size > 10_000, p
