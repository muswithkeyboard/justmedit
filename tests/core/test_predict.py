"""Тесты пакетной расшифровки файлов (io/table.py): обе схемы файла кейса, отклонение строк без тихих поправок,
режим auto, метрики по меткам, защита от формул, скорость.

Тесты на файле кейса вызывают helpers.case_data_path() — без файла это SKIP. Временные файлы создаются
в системном временном каталоге и удаляются; в репозиторий строки кейса не попадают.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

from helpers import case_data_path

from deficitlens_core import constants as C
from deficitlens_core.io.table import (OUTPUT_COLUMNS, escape_cell, predict_file, predict_table, read_table,
                                       write_table)
from deficitlens_core.ml.case_model import CaseModel, decode_profiles
from deficitlens_core.ml.features import to_matrix
from deficitlens_core.ml.train_case import GOLDEN_SYNTHETIC


def _synthetic_frame(n: int = 6) -> pd.DataFrame:
    """Придуманная таблица в схеме кейса (не данные пациентов)."""
    rows = []
    for i in range(n):
        base = dict(GOLDEN_SYNTHETIC[i % len(GOLDEN_SYNTHETIC)])
        rows.append({"patient_id": f"S{i + 1:03d}", **base})
    return pd.DataFrame(rows)


def _case_frame() -> pd.DataFrame:
    return read_table(case_data_path())


def _run(df: pd.DataFrame, name: str = "in.csv", **kwargs) -> tuple[pd.DataFrame, dict]:
    """Записывает таблицу во временный файл, запускает predict_file, возвращает (предсказания, сводка)."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out, summ = Path(tmp) / name, Path(tmp) / "pred.csv", Path(tmp) / "summary.json"
        df.to_csv(src, index=False)
        summary = predict_file(str(src), str(out), summary_path=str(summ), **kwargs)
        pred = pd.read_csv(out, dtype=str, keep_default_na=False)
        assert json.loads(summ.read_text(encoding="utf-8"))["rows_total"] == summary["rows_total"]
    return pred, summary


# --------------------------------------------------------------------------------------------
# Файл кейса
# --------------------------------------------------------------------------------------------
def test_predict_file_case_840_done():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "pred.csv"
        summary = predict_file(case_data_path(), str(out))
        pred = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert list(pred.columns) == OUTPUT_COLUMNS
    assert len(pred) == 840 and (pred["status"] == "done").all() and (pred["reject_reason"] == "").all()
    assert summary["rows_total"] == 840 and summary["rows_done"] == 840 and summary["rows_rejected"] == 0
    assert summary["mode"] == "benchmark" and summary["mode_requested"] == "auto"
    assert 0.38 <= summary["auto"]["measured_share"] <= 0.58
    assert set(pred["case_group"]) <= set(C.GROUPS5)
    assert set(pred["anemia_class"]) <= set(C.CLASSES12)
    assert set(pred["deficiency_cause"]) <= set(C.CAUSES11)
    assert set(pred["anemia"]) == {"0", "1"} and set(pred["hidden_deficiency"]) <= {"0", "1"}
    assert ((pred["anemia"] == "0") == (pred["case_group"] == "healthy")).all()
    assert ((pred["anemia"] == "1") | (pred["hidden_deficiency"].isin(["0", "1"]))).all()
    assert (pred.loc[pred["anemia"] == "1", "hidden_deficiency"] == "0").all()   # флаг только при отсутствии анемии
    conf = pred["confidence"].astype(float)
    assert conf.between(0, 1).all() and all(len(v.split(".")[-1]) <= 3 for v in pred["confidence"])
    m = summary["metrics"]
    assert m["level1"]["match"] == 840 and m["level1"]["accuracy"] == 1.0
    assert m["label_scheme"] == "classes12" and m["selected_mode"] == "benchmark"
    for mode in ("clinical", "benchmark"):                      # при наличии меток — метрики обоих режимов
        assert {"groups5", "classes12", "cause"} <= set(m["by_mode"][mode])
        assert np.array(m["by_mode"][mode]["groups5"]["confusion"]["matrix"]).shape == (5, 5)
    # столбцы-флаги дефицитов (метки) в признаки не попали: они перечислены как проигнорированные
    ignored = summary["columns"]["ignored_service"] + summary["columns"]["ignored_unknown"]
    assert "iron_deficiency" in ignored
    assert set(summary["columns"]["recognized"].values()) == set(C.FEATS)
    assert "seconds" in summary and summary["seconds"] > 0


def test_predict_file_other_schema():
    """Схема из текста кейса: anemia_cause вместо deficiency_cause, служебные record_origin и age_group."""
    df = _case_frame().rename(columns={"deficiency_cause": "anemia_cause"})
    df["record_origin"] = "synthetic"
    df["age_group"] = "adult"
    pred, summary = _run(df)
    assert len(pred) == 840 and (pred["status"] == "done").all()
    assert summary["columns"]["labels"]["cause"] == "anemia_cause"
    assert {"record_origin", "age_group"} <= set(summary["columns"]["ignored_service"])
    assert "cause" in summary["metrics"]["by_mode"][summary["mode"]]


def test_file_of_20_rows_is_clinical():
    pred, summary = _run(_case_frame().iloc[:20])
    assert summary["mode"] == "clinical" and (pred["mode"] == "clinical").all()
    assert summary["rows_done"] == 20


def test_explicit_mode_is_respected():
    df = _case_frame().iloc[:20]
    for mode in ("clinical", "benchmark"):
        pred, summary = _run(df, mode=mode)
        assert summary["mode"] == mode and (pred["mode"] == mode).all()


def test_formula_cells_are_escaped():
    df = _case_frame().iloc[:40].copy()
    ids = df["patient_id"].tolist()
    ids[0], ids[1], ids[2], ids[3] = "=1+1", "+7999", "-5", "@SUM(A1)"
    df["patient_id"] = ids
    pred, _ = _run(df)
    assert pred["patient_id"].tolist()[:4] == ["'=1+1", "'+7999", "'-5", "'@SUM(A1)"]
    assert pred["patient_id"].tolist()[4] == ids[4]
    assert not any(str(v).startswith(("=", "+", "-", "@")) for col in pred.columns for v in pred[col])


def test_hb_500_is_rejected():
    df = _case_frame().iloc[:40].copy()
    hb = df["hemoglobin"].tolist()
    hb[3] = "500"
    df["hemoglobin"] = hb
    pred, summary = _run(df)
    assert pred.loc[3, "status"] == "rejected" and "hemoglobin" in pred.loc[3, "reject_reason"]
    assert "500" in pred.loc[3, "reject_reason"]
    assert pred.loc[3, "anemia_class"] == "" and pred.loc[3, "anemia"] == ""    # никакой тихой поправки
    assert (pred.drop(index=3)["status"] == "done").all()
    assert summary["rows_rejected"] == 1 and summary["reject_reasons"] == {"out_of_hard_range": 1}


def test_labels_in_5_group_scheme():
    """Метки уже в схеме 5 групп (значения из class_aliases): метрики по 5 группам, без 12 классов."""
    df = _case_frame().copy()
    to_group = {"iron_deficiency_anemia": "iron_deficiency_anemia", "B12_deficiency_anemia": "vitamin_B12_deficiency_anemia",
                "anemia_other": "unexplained_anemia", "inflammation_anemia": "unexplained_anemia"}
    anemia = df["anemia"].astype(int).to_numpy()
    df["anemia_class"] = [("healthy" if a == 0 else to_group.get(c, "other_deficiency_anemia"))
                          for c, a in zip(df["anemia_class"], anemia)]
    pred, summary = _run(df.drop(columns=["anemia"]))
    m = summary["metrics"]
    assert m["label_scheme"] == "groups5"
    assert "classes12" not in m["by_mode"]["benchmark"] and m["by_mode"]["benchmark"]["groups5"]["n"] == 840
    assert m["level1"]["match"] == 840                           # анемия выведена из группы и совпала с ВОЗ
    assert "unrecognized_labels" not in m


def test_table_matches_model():
    """Таблица результата согласована с моделью: тот же класс и причина, что даёт decode_profiles."""
    df = _case_frame().iloc[:120]
    result, summary = predict_table(df, mode="clinical")
    X = to_matrix(pd.read_csv(case_data_path()).iloc[:120])
    dec = decode_profiles(CaseModel.load().predict_profiles(X, "clinical"))
    assert result["anemia_class"].tolist() == [C.CLASSES12[i] for i in dec["class12"]]
    assert result["deficiency_cause"].tolist() == [C.CAUSES11[i] for i in dec["cause11"]]
    assert np.allclose(result["confidence"].to_numpy(dtype=float), np.round(dec["confidence"], 3))


def test_speed_10000_rows():
    """predict_table на 10 000 строк (повтор строк кейса) быстрее 30 с."""
    df = _case_frame()
    big = pd.concat([df] * 12, ignore_index=True).iloc[:10000]
    t0 = time.perf_counter()
    result, summary = predict_table(big, mode="auto")
    seconds = time.perf_counter() - t0
    assert len(result) == 10000 and summary["rows_done"] == 10000
    assert seconds < 30, seconds


# --------------------------------------------------------------------------------------------
# Придуманные таблицы (без файла кейса)
# --------------------------------------------------------------------------------------------
def test_rejected_rows_have_reasons():
    df = _synthetic_frame(9).astype(object)
    df.loc[0, "sex"] = "X"                       # пол не распознан
    df.loc[1, "age_years"] = 17                  # младше 18
    df.loc[2, "hemoglobin"] = None               # гемоглобина нет
    df.loc[3, "MCV"] = "<80"                     # не число
    df.loc[4, "ferritin"] = -3                   # за жёсткой границей
    df.loc[5, "hemoglobin"] = 500                # гемоглобин 500 г/л: невозможное значение, не «опечатка 50,0»
    result, summary = predict_table(df)
    status = result["status"].tolist()
    assert status[:6] == ["rejected"] * 6 and status[6:] == ["done"] * 3
    reasons = result["reject_reason"].tolist()
    assert "пол" in reasons[0] and "18" in reasons[1] and "гемоглобин" in reasons[2]
    assert "MCV" in reasons[3] and "ferritin" in reasons[4]
    assert "hemoglobin" in reasons[5] and "500" in reasons[5]
    assert all(r == "" for r in reasons[6:])
    assert result.loc[:5, "anemia_class"].eq("").all() and result.loc[:5, "anemia"].isna().all()
    assert summary["rows_done"] == 3 and summary["rows_rejected"] == 6
    assert summary["reject_reasons"] == {"sex_unrecognized": 1, "age_below_18": 1, "hemoglobin_missing": 1,
                                         "not_a_number": 1, "out_of_hard_range": 2}
    assert summary["mode"] == "clinical"         # меньше 30 принятых строк
    # в сводке — только счётчики и названия, значений анализов нет
    assert "<80" not in json.dumps(summary, ensure_ascii=False)


def test_sex_and_column_name_variants():
    """Регистр и пробелы в именах столбцов не важны; пол принимается в вариантах из class_mapping.yaml."""
    df = _synthetic_frame(4).rename(columns={"hemoglobin": " Hemoglobin ", "MCV": "mcv", "sex": "SEX",
                                             "patient_id": "ID"})
    df["SEX"] = ["female", "М", "ж", "male"]
    result, summary = predict_table(df)
    assert (result["status"] == "done").all()
    assert summary["columns"]["recognized"]["Hemoglobin"] == "hemoglobin"
    assert summary["columns"]["recognized"]["mcv"] == "MCV" and summary["columns"]["id"] == "ID"
    assert result["patient_id"].tolist() == ["S001", "S002", "S003", "S004"]


def test_semicolon_cp1251_decimal_comma():
    """CSV из русского Excel: разделитель «;», кодировка cp1251, десятичная запятая."""
    df = _synthetic_frame(3)
    df["комментарий"] = "проверка"
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "ru.csv", Path(tmp) / "pred.csv"
        df.to_csv(src, index=False, sep=";", decimal=",", encoding="cp1251")
        back = read_table(src)
        assert back.attrs["source"] == {"format": "csv", "encoding": "cp1251", "delimiter": ";"}
        summary = predict_file(str(src), str(out))
        pred = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert (pred["status"] == "done").all() and summary["rows_done"] == 3
    assert "комментарий" in summary["columns"]["ignored_unknown"]
    assert any("запятая" in n for n in summary["notes"])
    direct, _ = predict_table(df.drop(columns=["комментарий"]))
    assert pred["anemia_class"].tolist() == direct["anemia_class"].tolist()
    assert pred["confidence"].astype(float).tolist() == direct["confidence"].tolist()


def test_xlsx_input_and_output():
    df = _synthetic_frame(5)
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "in.xlsx", Path(tmp) / "pred.xlsx"
        df.to_excel(src, index=False)
        summary = predict_file(str(src), str(out), mode="clinical")
        pred = pd.read_excel(out, dtype=str)
    assert summary["input"]["format"] == "xlsx" and summary["rows_done"] == 5
    assert list(pred.columns) == OUTPUT_COLUMNS and (pred["status"] == "done").all()
    direct, _ = predict_table(df, mode="clinical")
    assert pred["anemia_class"].tolist() == direct["anemia_class"].tolist()


def test_age_from_birth_and_sample_dates():
    df = _synthetic_frame(3).drop(columns=["age_years"])
    df["birth_date"] = ["1990-06-15", "15.06.1960", "2015-01-01"]
    df["anchor_sample_date"] = ["2026-06-14", "15.06.2026", "2026-01-01"]
    result, summary = predict_table(df)
    assert result["status"].tolist() == ["done", "done", "rejected"]          # 35 лет, 66 лет, 11 лет
    assert "18" in result.loc[2, "reject_reason"]
    assert summary["columns"]["dates"] == ["birth_date", "anchor_sample_date"]
    with_age = _synthetic_frame(3)
    with_age["age_years"] = [35, 66, 30]
    direct, _ = predict_table(with_age)
    assert result.loc[:1, "confidence"].tolist() == direct.loc[:1, "confidence"].tolist()


def test_auto_mode_rules():
    """От 30 строк режим бенчмарка включается только при доле сданных анализов 0,38–0,58."""
    cbc_only = pd.DataFrame([{"patient_id": f"C{i}", **GOLDEN_SYNTHETIC[0], "age_years": 20 + i} for i in range(40)])
    result, summary = predict_table(cbc_only, mode="auto")
    assert summary["rows_done"] == 40 and summary["auto"]["measured_share"] == 0.0
    assert summary["mode"] == "clinical" and (result["mode"] == "clinical").all()
    assert summary["completeness"] == {"cbc": 40, "cbc_iron_crp": 0, "standard": 0, "extended": 0}
    assert summary["metrics"] is None                         # меток в файле нет
    try:
        predict_table(cbc_only, mode="fast")
    except ValueError:
        pass
    else:
        raise AssertionError("неизвестный режим должен давать ValueError")


def test_unrecognized_labels_are_listed():
    df = _synthetic_frame(6)
    df["anemia"] = [0, 1, 1, 0, 1, 1]
    df["anemia_class"] = ["healthy", "IDA", "что-то своё", "norm", "unexplained", "other"]
    df["anemia_cause"] = ["", "iron", "мистика", "none", "unknown", "b12+folate"]
    result, summary = predict_table(df)
    m = summary["metrics"]
    assert m["label_scheme"] == "groups5"
    assert m["unrecognized_labels"] == {"anemia_class": ["что-тосвоё"], "cause": ["мистика"]}
    assert m["by_mode"]["clinical"]["groups5"]["n"] == 5 and m["by_mode"]["clinical"]["cause"]["n"] == 5
    assert m["level1"]["n"] == 6
    assert set(m["by_mode"]) == {"clinical", "benchmark"} and m["selected_mode"] == "clinical"


def test_escape_and_write():
    assert escape_cell("=cmd") == "'=cmd" and escape_cell("@x") == "'@x" and escape_cell("+1") == "'+1"
    assert escape_cell("-1") == "'-1" and escape_cell("P001") == "P001" and escape_cell(-1.5) == -1.5
    df = pd.DataFrame({"patient_id": ["=A1", "ok"], "confidence": [0.5, -0.25]})
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.csv"
        write_table(df, path)
        text = path.read_text(encoding="utf-8")
    # все ячейки в кавычках (QUOTE_ALL); текст-формула — с апострофом, числа не экранируются
    assert text.splitlines() == ['"patient_id","confidence"', '"\'=A1","0.5"', '"ok","-0.25"']


def test_empty_and_all_rejected_tables():
    empty = _synthetic_frame(2).iloc[:0]
    result, summary = predict_table(empty)
    assert len(result) == 0 and list(result.columns) == OUTPUT_COLUMNS and summary["rows_total"] == 0
    no_sex = _synthetic_frame(3).drop(columns=["sex"])
    result, summary = predict_table(no_sex)
    assert (result["status"] == "rejected").all() and summary["rows_done"] == 0
    assert any("sex" in n for n in summary["notes"])


def test_mapping_file_overrides():
    """Файл сопоставления (--mapping): свои имена столбцов и своя свёртка классов в группы."""
    df = _synthetic_frame(6).rename(columns={"hemoglobin": "Hb_g_l", "patient_id": "карта"})
    with tempfile.TemporaryDirectory() as tmp:
        src, out, mp = Path(tmp) / "in.csv", Path(tmp) / "pred.csv", Path(tmp) / "m.yaml"
        df.to_csv(src, index=False)
        base = predict_file(str(src), str(out))
        assert base["rows_done"] == 0 and "Hb_g_l" in base["columns"]["ignored_unknown"]   # без сопоставления Hb не найден
        mp.write_text("columns: {Hb_g_l: hemoglobin, карта: patient_id}\n"
                      "class_to_group5_if_anemia: {inflammation_anemia: other_deficiency_anemia}\n", encoding="utf-8")
        summary = predict_file(str(src), str(out), mapping_path=str(mp))
        pred = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert summary["rows_done"] == 6 and summary["columns"]["recognized"]["Hb_g_l"] == "hemoglobin"
    assert summary["columns"]["id"] == "карта" and pred["patient_id"].tolist()[0] == "S001"
    inflammation = pred["anemia_class"] == "inflammation_anemia"
    assert inflammation.any() and (pred.loc[inflammation, "case_group"] == "other_deficiency_anemia").all()
