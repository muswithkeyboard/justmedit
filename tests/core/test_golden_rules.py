"""Золотые случаи data/demo/examples.json: часть ожиданий, которая зависит только от нормализации, уровня 1 и правил
(anemia, severity, flags_include, checklist, urgent; для случая 1 после followup — rule_signals_include).
Ожидания, зависящие от моделей (скрининг, уровень 2), проверяет координатор на собранном движке.

Исключение — флаг b12_gray_zone (случай 4). При выключенных зарубежных порогах (norms_ru.yaml ->
policy.use_foreign: false) правила его не ставят: «пограничный B12» определяет движок по неуверенности модели.
Поэтому это ожидание проверяется через engine.analyze с моделями из models/; моделей нет — SKIP."""
from __future__ import annotations

import copy

import helpers
from helpers import load_examples

from deficitlens_core.config import load_config
from deficitlens_core.level1 import assess_level1
from deficitlens_core.normalize import normalize_input
from deficitlens_core.rules import evaluate_rules, use_foreign
from deficitlens_core.schemas import AnalysisInput

CFG = load_config()
EXAMPLES = {e["id"]: e for e in load_examples()}
# Флаги, которые при текущем правиле источников ставит движок (по модели), а не правила.
ENGINE_FLAGS = set() if use_foreign(CFG) else {"b12_gray_zone"}


def by_engine(ex: dict) -> bool:
    """Ожидания этого примера (флаги и рекомендованные анализы) зависят от флага, который ставит движок."""
    return bool(ENGINE_FLAGS & set(ex["expect"].get("flags_include", [])))


def pipeline(payload: dict):
    norm = normalize_input(AnalysisInput.model_validate(payload), CFG)
    level1 = assess_level1(norm, CFG)
    return norm, level1, evaluate_rules(norm, level1, CFG)


def test_examples_file_has_eight_cases():
    assert len(EXAMPLES) == 8


def test_golden_anemia_and_severity():
    for ex in EXAMPLES.values():
        _, level1, _ = pipeline(ex["input"])
        expect = ex["expect"]
        assert level1.anemia is expect["anemia"], ex["id"]
        if "severity" in expect:
            assert level1.severity == expect["severity"], ex["id"]
        if not expect["anemia"]:
            assert level1.severity is None, ex["id"]


def test_golden_flags():
    checked = 0
    for ex in EXAMPLES.values():
        _, _, rules = pipeline(ex["input"])
        codes = {f.code for f in rules.flags}
        for code in ex["expect"].get("flags_include", []):
            if code in ENGINE_FLAGS:
                continue                      # проверяется в test_golden_engine_flags
            assert code in codes, (ex["id"], code, codes)
            checked += 1
    assert checked >= 4


def test_golden_engine_flags():
    """Флаги, которые ставит движок по модели (b12_gray_zone при выключенных порогах B12), и анализы к ним."""
    examples = [ex for ex in EXAMPLES.values() if by_engine(ex)]
    if not examples:
        return                                # пороги включены: всё проверено в правилах
    from deficitlens_core.engine import analyze, load_models
    models = load_models()
    if models.case is None:
        helpers.skip("модель кейса не загружена: флаг b12_gray_zone при выключенных порогах ставит движок по модели")
    for ex in examples:
        result = analyze(AnalysisInput.model_validate(ex["input"]), models=models, cfg=CFG)
        codes = {f.code for f in result.flags}
        for code in ex["expect"]["flags_include"]:
            assert code in codes, (ex["id"], code, codes)
        wanted = ex["expect"].get("next_tests_include_any")
        if wanted:
            assert set(wanted) & {t.analyte for t in result.next_tests}, (ex["id"], wanted)
        assert result.level1.anemia is ex["expect"]["anemia"]


def test_golden_checklist():
    for ex in EXAMPLES.values():
        expected = ex["expect"].get("checklist")
        if not expected:
            continue
        _, _, rules = pipeline(ex["input"])
        status = {i.code: i.status for i in rules.checklist}
        for code, value in expected.items():
            assert status.get(code) == value, (ex["id"], code, status)


def test_golden_urgent():
    for ex in EXAMPLES.values():
        _, level1, _ = pipeline(ex["input"])
        assert bool(level1.urgent) is bool(ex["expect"].get("urgent", False)), ex["id"]


def test_golden_recommended_tests_by_rules():
    """«next_tests_include_any» в части правил: нужный анализ есть среди первых пяти обязательных (столько показывает движок)."""
    for ex in EXAMPLES.values():
        wanted = ex["expect"].get("next_tests_include_any")
        if not wanted or by_engine(ex):       # анализы к флагу движка — в test_golden_engine_flags
            continue
        _, _, rules = pipeline(ex["input"])
        top = [t["analyte"] for t in rules.mandatory_tests][:5]
        assert set(wanted) & set(top), (ex["id"], wanted, top)


def test_golden_case1_followup_gives_rule_signal():
    ex = EXAMPLES["case1_hidden_iron"]
    norm, level1, rules = pipeline(ex["input"])
    assert level1.anemia is False and rules.rule_signals == [] and rules.flags == []
    payload = copy.deepcopy(ex["input"])
    payload["values"].update(ex["followup"]["add_values"])
    norm, level1, rules = pipeline(payload)
    codes = {s.code for s in rules.rule_signals}
    for code in ex["expect"]["after_followup"]["rule_signals_include"]:
        assert code in codes, codes
    assert level1.anemia is False


def test_golden_case6_mentzer_index():
    _, _, rules = pipeline(EXAMPLES["case6_microcytosis_not_iron"]["input"])
    assert rules.derived["mentzer"] == 16.4


def test_golden_case8_pregnancy_threshold():
    norm, level1, rules = pipeline(EXAMPLES["case8_pregnancy"]["input"])
    assert level1.threshold_g_l == 105 and level1.anemia is False
    assert rules.flags == [] and rules.rule_signals == [] and rules.mandatory_tests == []


def test_golden_examples_are_arithmetically_consistent():
    """Проверка самих примеров: MCH = Hb / RBC; MCHC = Hb / Hct × 100; MCV = Hct / RBC × 10 (допуск на округление)."""
    for ex in EXAMPLES.values():
        v = ex["input"]["values"]
        if all(k in v for k in ("hemoglobin", "RBC", "hematocrit", "MCV", "MCH", "MCHC")):
            assert abs(v["hemoglobin"] / v["RBC"] - v["MCH"]) < 0.3, ex["id"]
            assert abs(v["hemoglobin"] / v["hematocrit"] * 100 - v["MCHC"]) < 2.5, ex["id"]
            assert abs(v["hematocrit"] / v["RBC"] * 10 - v["MCV"]) < 1.0, ex["id"]
