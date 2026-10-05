"""Рисунки для слайдов и docs/pitch/numbers.json — все числа слайдов с источником.

    uv run python scripts/make_figures.py                 # рисунки + numbers.json
    uv run python scripts/make_figures.py --only numbers  # только numbers.json
    uv run python scripts/make_figures.py --list          # список рисунков и источников

Источники (модели не переобучаются, в models/ ничего не пишется):
  docs/metrics/case_metrics.json   — кросс-валидация модели case-v1 (5 фолдов × 3 повтора);
  docs/metrics/screen_metrics.json — временная проверка screen-v1 на NHANES (обучение 1999–2016, тест 2017–2023);
  models/*/metadata.json           — версия и дата обучения моделей (для поля source);
  docs/perf.md                     — замер scripts/perf.py (время разбора и пакета).
Два рисунка (скрытый дефицит по полу и возрасту, sTfR при воспалении) и квартили RDW считаются по файлу NHANES
(DL_NHANES_DATA) — только взвешенные доли и квартили, без моделей. Без файла эти рисунки пропускаются, а числа
по ним в numbers.json переносятся из прежнего numbers.json, если он есть. В репозиторий попадают только агрегаты.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT / "docs" / "pitch" / "figures"
NUMBERS_JSON = ROOT / "docs" / "pitch" / "numbers.json"
CASE_METRICS = ROOT / "docs" / "metrics" / "case_metrics.json"
SCREEN_METRICS = ROOT / "docs" / "metrics" / "screen_metrics.json"
CASE_META = ROOT / "models" / "case-v1" / "metadata.json"
SCREEN_META = ROOT / "models" / "screen-v1" / "metadata.json"
PERF_MD = ROOT / "docs" / "perf.md"

# Палитра проекта: акцент #21108E, вторичный #5B5A6E, линии #DCDAE8; фон белый. Светлый тон акцента — третья
# серия, серый — базовая линия (типовой бустинг, сырые значения). Серии различаются не только цветом:
# на каждом столбце подпись значения, у линий — разные маркеры и подписи.
ACCENT = "#21108E"
ACCENT_LIGHT = "#7B6FD0"
SECONDARY = "#5B5A6E"
BASELINE = "#A9A7B8"
GRID = "#DCDAE8"
INK = "#1B1A24"

CASE_NAME = "Учебный набор кейса (840 строк)"
LEVELS = ["cbc", "cbc_iron_crp", "standard", "extended"]
LEVEL_RU = ["Только ОАК", "+ железо, СРБ", "+ B12, фолаты", "Строка как есть"]
GROUP_RU = {"healthy": "Нет анемии", "iron_deficiency_anemia": "Железо-\nдефицитная",
            "vitamin_B12_deficiency_anemia": "B12-\nдефицитная", "unexplained_anemia": "Неясная\n(в т. ч. воспаление)",
            "other_deficiency_anemia": "Другая\nдефицитная"}
GROUP_SHORT_RU = {**GROUP_RU, "unexplained_anemia": "Неясная"}
SCREEN_GROUP_RU = {"ALL": "Все взрослые", "F18-49": "Ж 18–49", "F50+": "Ж 50+", "M": "Мужчины"}
GKEY = {"F18-49": "F18_49", "F50+": "F50", "M": "M", "ALL": "ALL"}


class Skip(Exception):
    """Рисунок не строится (нет данных) — сообщение печатается, остальные рисунки строятся."""


# ---------------------------------------------------------------------------
# Чтение источников
# ---------------------------------------------------------------------------


def load_json(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def load_ctx() -> dict[str, Any]:
    return {"case": load_json(CASE_METRICS), "screen": load_json(SCREEN_METRICS),
            "case_meta": load_json(CASE_META), "screen_meta": load_json(SCREEN_META)}


def nhanes_path() -> Path | None:
    p = os.environ.get("DL_NHANES_DATA")
    if p and Path(p).is_file():
        return Path(p)
    return None


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def nhanes(ctx: dict[str, Any]) -> dict[str, Any]:
    """Когорта NHANES так же, как в deficitlens_core.ml.screen.load_cohort (без обучения моделей).

    Дополнительно читаются sTfR и СРБ (для рисунка про воспаление) у тех же небеременных взрослых
    с полным ОАК. Проверяется, что файл тот же, по которому посчитан screen_metrics.json (SHA-256).
    """
    if "nh" in ctx:
        return ctx["nh"]
    p = nhanes_path()
    if p is None:
        raise Skip("нет файла NHANES: задайте DL_NHANES_DATA (абсолютный путь к nhanes_deficiency_real.csv)")
    sys.path.insert(0, str(ROOT / "src"))
    import pandas as pd

    from deficitlens_core.constants import CBC_LABS
    from deficitlens_core.ml import screen

    t0 = time.perf_counter()
    sha = sha256_file(p)
    expected = ctx["screen"]["data"]["sha256"]
    if sha != expected:
        print(f"  ВНИМАНИЕ: SHA-256 файла NHANES {sha[:12]}… не совпадает с screen_metrics.json {expected[:12]}…")
    adults, cohort, counts = screen.load_cohort(p)
    extra = pd.read_csv(p, usecols=["age_years", "pregnant", *CBC_LABS, "sTfR", "CRP"], low_memory=False)
    extra = extra[(extra.age_years >= screen.ADULT_AGE) & (extra.pregnant == 0)].dropna(subset=CBC_LABS)
    adults = adults.join(extra[["sTfR", "CRP"]])
    ctx["nh"] = {"adults": adults, "cohort": cohort, "counts": counts, "sha256": sha, "sha_ok": sha == expected,
                 "wmean": screen.wmean}
    print(f"  NHANES: {len(adults)} небеременных взрослых с полным ОАК, {time.perf_counter() - t0:.1f} с")
    return ctx["nh"]


# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------


def setup_style() -> None:
    import matplotlib as mpl

    mpl.rcParams.update({
        "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
        "font.size": 17, "axes.titlesize": 22, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.labelsize": 18, "xtick.labelsize": 16, "ytick.labelsize": 16, "legend.fontsize": 16,
        "axes.edgecolor": GRID, "axes.linewidth": 1.2, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 1,
        "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
        "text.color": INK, "axes.labelcolor": INK, "xtick.color": SECONDARY, "ytick.color": SECONDARY,
        "legend.frameon": False, "svg.fonttype": "path", "font.family": "DejaVu Sans",
        "svg.hashsalt": "deficitlens",
    })


def new_fig(w: float = 13.33, h: float = 7.5) -> tuple[Any, Any]:
    import matplotlib.pyplot as plt

    return plt.subplots(figsize=(w, h))


def note(fig: Any, text: str, width: int = 125, y: float = -0.01) -> None:
    """Подпись об источнике под рисунком, с переносом строк (ширина рисунка не растёт)."""
    import textwrap

    w = int(width * fig.get_figwidth() / 13.33)
    fig.text(0.01, y, textwrap.fill(text, w), fontsize=12, color=SECONDARY, ha="left", va="top")


def save(fig: Any, name: str, out_dir: Path) -> list[Path]:
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / f"{name}.png", out_dir / f"{name}.svg"]
    fig.savefig(paths[0], dpi=150, bbox_inches="tight", metadata={"Software": None})
    fig.savefig(paths[1], bbox_inches="tight", metadata={"Date": None, "Creator": None})
    plt.close(fig)
    return paths


def pct(x: float, nd: int = 0) -> str:
    return f"{100 * x:.{nd}f} %".replace(".", ",")


def dec(x: float, nd: int = 2) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def pct_axis(ax: Any, which: str = "y") -> None:
    import matplotlib.pyplot as plt

    fmt = plt.FuncFormatter(lambda v, _: f"{round(v * 100, 1):g} %".replace(".", ",") if v >= 0 else "")
    (ax.yaxis if which == "y" else ax.xaxis).set_major_formatter(fmt)


# ---------------------------------------------------------------------------
# Рисунки по учебному набору кейса
# ---------------------------------------------------------------------------


def case_src(ctx: dict[str, Any]) -> str:
    c = ctx["case"]["config"]
    return (f"docs/metrics/case_metrics.json (модель {c['model']}, обучена {ctx['case_meta']['trained_at'][:10]}; "
            f"CV {c['n_splits']}×{c['n_repeats']}, seed {c['random_state']})")


def fig_accuracy_by_level(out: Path, ctx: dict[str, Any]) -> list[Path]:
    """Точность по 12 классам по уровням полноты: типовой бустинг / клинический режим / режим бенчмарка."""
    bml = ctx["case"]["by_mode_level"]
    series = [("Типовой бустинг на всех столбцах", "naive", BASELINE),
              ("Клинический режим (без подсказки)", "clinical", ACCENT),
              ("Режим бенчмарка (ансамбль)", "benchmark", ACCENT_LIGHT)]
    fig, ax = new_fig()
    x = np.arange(len(LEVELS))
    wbar = 0.26
    for k, (label, mode, color) in enumerate(series):
        xs = x + (k - 1) * (wbar + 0.02)
        vals = [bml[mode][lv]["acc12"]["mean"] for lv in LEVELS]
        sds = [bml[mode][lv]["acc12"]["sd"] for lv in LEVELS]
        ax.bar(xs, vals, wbar, color=color, label=label, yerr=sds, ecolor=SECONDARY, capsize=4,
               error_kw={"lw": 1.2})
        for xi, v, s in zip(xs, vals, sds, strict=True):
            ax.text(xi, v + s + 0.012, dec(v), ha="center", va="bottom", fontsize=14, color=INK)
    ax.set_xticks(x, LEVEL_RU)
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("Точность, 12 классов")
    ax.grid(axis="x", visible=False)
    ax.set_title("На неполной панели клинический режим точнее типового бустинга")
    ax.legend(loc="upper left", ncol=1)
    note(fig, f"{CASE_NAME}. Кросс-валидация 5 фолдов × 3 повтора; столбец — среднее по 15 тестовым фолдам, "
              "отрезок — стандартное отклонение по фолдам (разброс, не доверительный интервал). "
              f"Источник: {case_src(ctx)}, раздел by_mode_level.")
    return save(fig, "accuracy_by_level", out)


def _confusion(out: Path, ctx: dict[str, Any], mode: str, name: str, title: str) -> list[Path]:
    conf = ctx["case"]["confusion"][mode]
    labels = conf["groups5"]["labels"]
    cm = np.asarray(conf["groups5"]["matrix"], float)
    row = cm / cm.sum(axis=1, keepdims=True)
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("dl", ["#FFFFFF", GRID, ACCENT_LIGHT, ACCENT])
    fig, ax = new_fig(12, 9)
    ax.imshow(row, cmap=cmap, vmin=0, vmax=1)
    ax.grid(False)
    for i in range(5):
        for j in range(5):
            if cm[i, j] == 0:
                continue
            color = "white" if row[i, j] > 0.55 else INK
            ax.text(j, i, f"{int(cm[i, j])}\n{pct(row[i, j])}", ha="center", va="center", fontsize=15, color=color)
    ax.set_xticks(range(5), [GROUP_SHORT_RU[g] for g in labels], fontsize=14)
    ax.set_yticks(range(5), [GROUP_RU[g] for g in labels], fontsize=14)
    ax.set_xlabel("Предсказано")
    ax.set_ylabel("На самом деле")
    for sp in ax.spines.values():
        sp.set_visible(False)
    acc = np.trace(cm) / cm.sum()
    acc_an = np.trace(cm[1:, 1:]) / cm[1:].sum()
    ax.set_title(f"{title}: точность {pct(acc, 1)}, у строк с анемией {pct(acc_an, 1)}")
    m = ctx["case"]["by_mode_level"][mode][conf["level"]]
    note(fig, f"{CASE_NAME}; 5 групп кейса (config/class_mapping.yaml — сопоставление команды). Строка как есть. "
              f"Ячейки — внеблочные предсказания первого повтора кросс-валидации (n = {int(cm.sum())}, "
              f"с анемией {int(cm[1:].sum())}); доли — по строке. Среднее по 15 фолдам: точность "
              f"{dec(m['acc5']['mean'], 3)} ± {dec(m['acc5']['sd'], 3)}, у строк с анемией "
              f"{dec(m['acc5_anemic']['mean'], 3)} ± {dec(m['acc5_anemic']['sd'], 3)}. "
              f"Источник: {case_src(ctx)}, раздел confusion.{mode}.")
    return save(fig, name, out)


def fig_confusion_benchmark(out: Path, ctx: dict[str, Any]) -> list[Path]:
    return _confusion(out, ctx, "benchmark", "confusion5_benchmark", "Режим бенчмарка")


def fig_confusion_clinical(out: Path, ctx: dict[str, Any]) -> list[Path]:
    return _confusion(out, ctx, "clinical", "confusion5_clinical", "Клинический режим")


# ---------------------------------------------------------------------------
# Рисунки по NHANES
# ---------------------------------------------------------------------------


def screen_src(ctx: dict[str, Any]) -> str:
    return (f"docs/metrics/screen_metrics.json (модель screen-v1 от {ctx['screen_meta']['created'][:10]}; "
            "обучение 1999–2016, тест 2017–2018 и 2021–2023)")


def fig_referral(out: Path, ctx: dict[str, Any]) -> list[Path]:
    """«Сколько направили — сколько нашли»: точки таблицы направлений, запасная модель по Hb, точка правила."""
    sm = ctx["screen"]
    tab = sm["referral"]["tables"]["F18-49"]["t15"]
    base = sm["referral"]["baselines"]["F18-49"]["t15"]
    rc = sm["rule_comparison"]["by_group"]["F18-49"]["t15"]
    auc = sm["summary"]["by_group"]["F18-49"]["t15"]
    auc_hb = sm["summary"]["hb_only_F18-49"]["t15"]
    sh = np.array([0] + [r["refer_share"] for r in tab["full_cbc"]])
    cbc = np.array([0] + [r["sensitivity"] for r in tab["full_cbc"]])
    hb = np.array([0] + [r["sensitivity"] for r in tab["hb_only"]])
    rule_x, rule_y = rc["rule"]["referred_per1000"] / 1000, rc["rule"]["sensitivity"]
    model_y = rc["model_same_referrals"]["sensitivity"]
    lo, hi = rc["sensitivity_diff_ci95_pp"]
    fig, ax = new_fig()
    ax.plot(sh * 100, sh * 100, color=GRID, lw=2, ls="--", label="Случайный выбор")
    ax.plot(sh * 100, hb * 100, "s-", color=SECONDARY, lw=2, ms=8,
            label=f"Только Hb + возраст + пол (AUC {dec(auc_hb['auc'])})")
    ax.plot(sh * 100, cbc * 100, "o-", color=ACCENT, lw=3, ms=9, label=f"Модель по ОАК (AUC {dec(auc['auc'])})")
    ax.scatter([rule_x * 100], [rule_y * 100], s=170, color="white", edgecolor=SECONDARY, linewidth=3, zorder=5,
               label="Правило MCV <80 / MCH <27 / RDW >14,5")
    ax.annotate(f"правило: направили {pct(rule_x, 1)}, нашли {pct(rule_y)}", (rule_x * 100, rule_y * 100),
                xytext=(rule_x * 100 + 3, rule_y * 100 - 13), fontsize=15, color=SECONDARY,
                arrowprops={"arrowstyle": "-", "color": SECONDARY})
    ax.scatter([rule_x * 100], [model_y * 100], s=110, color=ACCENT, zorder=6)
    ax.annotate(f"модель при тех же направлениях: {pct(model_y)}\n(+{dec(rc['sensitivity_diff_pp'], 1)} п.п., "
                f"95 % ДИ {dec(lo, 1)}–{dec(hi, 1)})", (rule_x * 100, model_y * 100),
                xytext=(rule_x * 100 - 13, model_y * 100 + 9), fontsize=14, color=ACCENT)
    p30 = next(r for r in tab["full_cbc"] if r["refer_share"] == 0.3)
    ax.annotate(f"направили 30 % → нашли {pct(p30['sensitivity'])}", (30, p30["sensitivity"] * 100),
                xytext=(32, p30["sensitivity"] * 100 - 10), fontsize=15, color=ACCENT)
    ax.set_xlim(0, 52)
    ax.set_ylim(0, 102)
    ax.set_xlabel("Направили на ферритин, % женщин 18–49 с нормальным Hb")
    ax.set_ylabel("Нашли, % случаев ферритина <15")
    ax.set_title("Сколько направили — сколько нашли: порог двигается, правило — одна точка")
    ax.legend(loc="lower right")
    note(fig, f"NHANES (США), тест 2017–2023: женщины 18–49 без анемии по ВОЗ, n = {base['n']}, случаев ферритина "
              f"<15 — {base['cases']}; доли — с весами NHANES. AUC по ОАК {dec(auc['auc'], 3)} (95 % ДИ "
              f"{dec(auc['ci95'][0], 3)}–{dec(auc['ci95'][1], 3)}). Точки — таблица направлений 10–50 % "
              f"(между точками — линия, а не расчёт). Источник: {screen_src(ctx)}, разделы referral и rule_comparison.")
    return save(fig, "referral_curve", out)


def fig_calibration(out: Path, ctx: dict[str, Any]) -> list[Path]:
    """Калибровка до и после стандартизации ОАК: средняя предсказанная вероятность против наблюдаемой доли."""
    import matplotlib.pyplot as plt

    sm = ctx["screen"]
    rows = [r for r in sm["calibration_shift"]["rows"] if r["features"] == "full_cbc"]
    ns = {g: sm["cohort"]["test_no_anemia_by_group"].get(g, sm["summary"]["n_test"]) for g in SCREEN_GROUP_RU}
    fig, axes = plt.subplots(1, 2, figsize=(15, 7.5))
    for ax, tgt in zip(axes, ("t15", "t30"), strict=True):
        rr = [r for r in rows if r["target"] == tgt]
        groups = [g for g in SCREEN_GROUP_RU if any(r["group"] == g for r in rr)]
        by = {r["group"]: r for r in rr}
        x = np.arange(len(groups))
        for off, key, color, label in ((-0.2, "mean_predicted_raw", BASELINE, "Сырые значения ОАК"),
                                       (0.2, "mean_predicted_z", ACCENT, "После стандартизации")):
            v = [by[g][key] for g in groups]
            ax.bar(x + off, v, 0.38, color=color, label=label)
            for xi, val in zip(x, v, strict=True):
                ax.text(xi + off, val + 0.004, pct(val, 1), ha="center", va="bottom", fontsize=12)
        obs = [by[g]["observed"] for g in groups]
        ax.hlines(obs, x - 0.42, x + 0.42, color=INK, lw=3, label="Наблюдается")
        ax.set_xticks(x, [f"{SCREEN_GROUP_RU[g]}\nn={ns[g]}" for g in groups], fontsize=14)
        ax.grid(axis="x", visible=False)
        ax.set_title(f"Ферритин <{tgt[1:]}", loc="center", fontsize=20)
        pct_axis(ax)
        ax.set_ylim(0, max(max(by[g]["mean_predicted_raw"] for g in groups), max(obs)) * 1.18)
    axes[0].set_ylabel("Средняя вероятность / доля")
    axes[1].legend(loc="upper right")
    fig.suptitle("Смена анализатора: модель на сырых ОАК завышает риск, стандартизация возвращает калибровку",
                 x=0.01, ha="left", fontsize=21, fontweight="bold")
    f30 = next(r for r in rows if r["target"] == "t30" and r["group"] == "F18-49")
    note(fig, f"NHANES (США): обучение 1999–2016, тест 2017–2023, взрослые без анемии по ВОЗ; доли без весов. "
              f"Ж 18–49, <30: предсказано {dec(f30['mean_predicted_raw'])} → {dec(f30['mean_predicted_z'])}, "
              f"наблюдается {dec(f30['observed'])}; AUC {dec(f30['auc_raw'], 3)} → {dec(f30['auc_z'], 3)}. "
              "Стандартизация: z = (x − медиана) / МКР внутри цикла по полу, без меток. У мужчин для <15 случаев "
              f"меньше 15 — не оценивается. Источник: {screen_src(ctx)}, раздел calibration_shift.")
    fig.tight_layout(rect=(0, 0.02, 1, 0.94))
    return save(fig, "calibration_raw_vs_z", out)


def fig_rdw_shift(out: Path, ctx: dict[str, Any]) -> list[Path]:
    """Медиана RDW по циклам NHANES (screen_metrics.json); с файлом — ещё межквартильный размах."""
    med = ctx["screen"]["calibration_shift"]["rdw_median_by_cycle"]
    cycles = list(med)
    fig, ax = new_fig()
    x = np.arange(len(cycles))
    try:
        ad = nhanes(ctx)["adults"]
        g = ad.groupby("nhanes_cycle").RDW
        q25, q75 = g.quantile(0.25).reindex(cycles).to_numpy(), g.quantile(0.75).reindex(cycles).to_numpy()
        ax.fill_between(x, q25, q75, color=GRID, alpha=0.8, label="Межквартильный размах")
        n_by = g.size().reindex(cycles)
        src = (f"медианы — docs/metrics/screen_metrics.json (calibration_shift), квартили — DL_NHANES_DATA; "
               f"n по циклам {int(n_by.min())}–{int(n_by.max())}")
    except Skip as e:
        print(f"  RDW без квартилей ({e})")
        src = "docs/metrics/screen_metrics.json (calibration_shift)"
    ax.plot(x, [med[c] for c in cycles], "o-", color=ACCENT, lw=3, ms=10, label="Медиана RDW")
    for xi, c in zip(x, cycles, strict=True):
        ax.text(xi, med[c] + 0.12, dec(med[c], 1), ha="center", fontsize=14)
    k = cycles.index("2013-2014")
    ax.axvline(k - 0.5, color=SECONDARY, lw=2, ls=":")
    ax.text(k - 0.45, ax.get_ylim()[1] - 0.15, "смена анализатора", color=SECONDARY, fontsize=15, va="top")
    ax.set_xticks(x, [c.replace("-", "–") for c in cycles], rotation=35, ha="right")
    ax.set_ylabel("RDW, %")
    import matplotlib.pyplot as plt

    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: dec(v, 1)))
    ax.set_title("Один и тот же RDW значит разное на разных анализаторах")
    ax.legend(loc="upper left")
    note(fig, f"NHANES (США), небеременные взрослые 18+ с полным ОАК, по циклам обследования. Источник: {src}.",
         y=-0.07)
    return save(fig, "rdw_shift", out)


AGE_BANDS = [(18, 30, "18–29"), (30, 40, "30–39"), (40, 50, "40–49"), (50, 65, "50–64"), (65, 200, "65+")]


def hidden_by_sex_age(ctx: dict[str, Any]) -> dict[str, Any]:
    """Доли ферритина <15 и <30 у людей без анемии (тест 2017–2023), по полу и возрасту, с весами NHANES."""
    if "hidden" in ctx:
        return ctx["hidden"]
    nh = nhanes(ctx)
    c = nh["cohort"]
    te = c[c.is_test & (c.anemia_who == 0)]
    res: dict[str, Any] = {"n_total": len(te)}
    for sex in ("F", "M"):
        for lo, hi, lab in AGE_BANDS:
            d = te[(te.sex == sex) & (te.age_years >= lo) & (te.age_years < hi)]
            res[f"{sex}|{lab}"] = {"n": len(d), "cases_t15": int(d.t15.sum()), "cases_t30": int(d.t30.sum()),
                                   "t15": nh["wmean"](d.t15, d.w) if len(d) else float("nan"),
                                   "t30": nh["wmean"](d.t30, d.w) if len(d) else float("nan")}
    # Сверка с screen_metrics.json: доли в группе Ж 18–49 должны совпасть с cohort.prevalence_no_anemia_test_weighted.
    f = te[te.grp == "F18-49"]
    res["check_F18-49"] = {"n": len(f), "t15": nh["wmean"](f.t15, f.w), "t30": nh["wmean"](f.t30, f.w)}
    ctx["hidden"] = res
    return res


def fig_hidden_by_sex_age(out: Path, ctx: dict[str, Any]) -> list[Path]:
    """Доля скрытого дефицита железа (ферритин <15 и <30 при нормальном Hb) по полу и возрасту."""
    h = hidden_by_sex_age(ctx)
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(14, 7.5), sharey=True)
    x = np.arange(len(AGE_BANDS))
    for ax, sex, title in ((axes[0], "F", "Женщины"), (axes[1], "M", "Мужчины")):
        v30 = [h[f"{sex}|{lab}"]["t30"] for *_, lab in AGE_BANDS]
        v15 = [h[f"{sex}|{lab}"]["t15"] for *_, lab in AGE_BANDS]
        ax.bar(x - 0.2, v30, 0.38, color=ACCENT_LIGHT, label="ферритин <30")
        ax.bar(x + 0.2, v15, 0.38, color=ACCENT, label="ферритин <15 (ВОЗ)")
        for xi, a, b in zip(x, v30, v15, strict=True):
            if not np.isnan(a):
                ax.text(xi - 0.2, a + 0.005, pct(a, 1), ha="center", fontsize=11)
            if not np.isnan(b):
                ax.text(xi + 0.2, b + 0.005, pct(b, 1), ha="center", fontsize=11)
        ax.set_xticks(x, [lab for *_, lab in AGE_BANDS])
        ax.set_title(title, loc="center", fontsize=20)
        ax.grid(axis="x", visible=False)
        ax.set_xlabel("Возраст, лет")
        ns = ", ".join(str(h[f"{sex}|{lab}"]["n"]) for *_, lab in AGE_BANDS)
        ax.text(0.0, -0.2, f"n: {ns}", transform=ax.transAxes, fontsize=12, color=SECONDARY)
    axes[0].set_ylabel("Доля людей с нормальным Hb")
    pct_axis(axes[0])
    axes[0].legend(loc="upper right")
    fig.suptitle("Скрытый дефицит железа: гемоглобин в норме, ферритин низкий", x=0.01, ha="left",
                 fontsize=22, fontweight="bold")
    note(fig, f"NHANES 2017–2018 и 2021–2023 (США), небеременные взрослые без анемии по ВОЗ с измеренным "
              f"ферритином, n = {h['n_total']}; доли с весами NHANES. У мужчин ферритин измерен только в 2017–2018. "
              "Источник: scripts/make_figures.py по DL_NHANES_DATA (агрегаты; когорта как в screen.load_cohort).")
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    return save(fig, "hidden_deficiency_by_sex_age", out)


FER_BINS = [0, 15, 30, 70, 100, 1e9]
FER_LABELS = ["<15", "15–30", "30–70", "70–100", "≥100"]
FER_KEYS = {"<15": "lt15", "15–30": "15-30", "30–70": "30-70", "70–100": "70-100", "≥100": "100+"}


def stfr_by_ferritin(ctx: dict[str, Any]) -> dict[str, Any]:
    """Как в прототипе: повышенный sTfR — выше 95-го перцентиля (по полу) у людей без воспаления (СРБ ≤5 мг/л),
    с ферритином ≥50 мкг/л и без анемии по ВОЗ. Доли — с весами NHANES (mec_weight)."""
    if "stfr" in ctx:
        return ctx["stfr"]
    import pandas as pd

    nh = nhanes(ctx)
    ad = nh["adults"]
    b = ad[ad.ferritin_harmonized.notna() & ad.sTfR.notna() & ad.CRP.notna()].copy()
    refp = b[(b.CRP <= 5) & (b.ferritin_harmonized >= 50) & (b.anemia_who == 0)]
    cut = {s: float(refp[refp.sex == s].sTfR.quantile(0.95)) for s in ("F", "M")}
    b["high"] = (b.sTfR > b.sex.map(cut)).astype(float)
    b["bin"] = pd.cut(b.ferritin_harmonized, FER_BINS, right=False, labels=FER_LABELS)
    res: dict[str, Any] = {"cut_mg_l": cut, "n": len(b), "share_female": float((b.sex == "F").mean()),
                           "n_reference": len(refp)}
    for infl, mask in (("crp_gt5", b.CRP > 5), ("crp_le5", b.CRP <= 5)):
        for lab in FER_LABELS:
            q = b[mask & (b.bin == lab)]
            res[f"{infl}|{lab}"] = {"n": len(q), "share": nh["wmean"](q.high, q.w) if len(q) else float("nan")}
    ctx["stfr"] = res
    return res


def fig_stfr(out: Path, ctx: dict[str, Any]) -> list[Path]:
    """Ферритин под маской воспаления: доля с повышенным sTfR по диапазонам ферритина, СРБ >5 и ≤5."""
    s = stfr_by_ferritin(ctx)
    fig, ax = new_fig()
    x = np.arange(len(FER_LABELS))
    for off, key, color, label in ((-0.2, "crp_gt5", ACCENT, "СРБ >5 мг/л (воспаление)"),
                                   (0.2, "crp_le5", BASELINE, "СРБ ≤5 мг/л")):
        v = [s[f"{key}|{lab}"]["share"] for lab in FER_LABELS]
        ax.bar(x + off, v, 0.38, color=color, label=label)
        for xi, val, lab in zip(x, v, FER_LABELS, strict=True):
            ax.text(xi + off, val + 0.01, pct(val), ha="center", fontsize=14)
            ax.text(xi + off, -0.045, f"n={s[f'{key}|{lab}']['n']}", ha="center", fontsize=11, color=SECONDARY)
    ax.set_xticks(x, FER_LABELS)
    ax.tick_params(axis="x", pad=22)
    ax.set_xlabel("Ферритин, мкг/л")
    ax.set_ylabel("Доля с повышенным sTfR")
    ax.set_ylim(-0.06, max(s[f"crp_gt5|{lab}"]["share"] for lab in FER_LABELS) + 0.12)
    pct_axis(ax)
    ax.grid(axis="x", visible=False)
    ax.set_title("При воспалении «нормальный» ферритин 30–70 скрывает дефицит железа")
    ax.legend(loc="upper right")
    note(fig, f"NHANES (США), небеременные взрослые с полным ОАК, ферритином, sTfR и СРБ, n = {s['n']} (женщин "
              f"{pct(s['share_female'])}); доли с весами NHANES. Повышенный sTfR — выше 95-го перцентиля по полу "
              f"у людей без воспаления, с ферритином ≥50 и без анемии (n = {s['n_reference']}; порог Ж "
              f"{dec(s['cut_mg_l']['F'])}, М {dec(s['cut_mg_l']['M'])} мг/л). Источник: scripts/make_figures.py "
              "по DL_NHANES_DATA (агрегаты).")
    return save(fig, "stfr_inflammation", out)


FIGURES: dict[str, tuple[Callable[[Path, dict[str, Any]], list[Path]], str]] = {
    "accuracy_by_level": (fig_accuracy_by_level, "case_metrics.json: by_mode_level"),
    "confusion5_benchmark": (fig_confusion_benchmark, "case_metrics.json: confusion.benchmark"),
    "confusion5_clinical": (fig_confusion_clinical, "case_metrics.json: confusion.clinical"),
    "referral_curve": (fig_referral, "screen_metrics.json: referral, rule_comparison"),
    "calibration_raw_vs_z": (fig_calibration, "screen_metrics.json: calibration_shift"),
    "rdw_shift": (fig_rdw_shift, "screen_metrics.json: calibration_shift (+ DL_NHANES_DATA для квартилей)"),
    "hidden_deficiency_by_sex_age": (fig_hidden_by_sex_age, "DL_NHANES_DATA (агрегаты)"),
    "stfr_inflammation": (fig_stfr, "DL_NHANES_DATA (агрегаты)"),
}


# ---------------------------------------------------------------------------
# numbers.json
# ---------------------------------------------------------------------------

# Значения из docs/pitch/NUMBERS.md (состояние 04.10, 00:36) для сверки: ключ numbers.json → число в NUMBERS.md.
NUMBERS_MD: dict[str, float] = {
    "case.level1_match": 840,
    "case.benchmark.extended.acc12": 0.919, "case.benchmark.extended.acc12_sd": 0.020,
    "case.benchmark.extended.f1_12": 0.910, "case.benchmark.extended.acc5": 0.952,
    "case.benchmark.extended.f1_5": 0.940, "case.benchmark.extended.f1_cause": 0.893,
    "case.clinical.extended.acc12": 0.849, "case.clinical.extended.acc5": 0.914,
    "case.clinical.cbc_iron_crp.acc12": 0.665, "case.clinical.cbc.acc12": 0.573, "case.clinical.cbc.acc5": 0.782,
    "case.clinical.cbc.auroc_hidden": 0.897, "case.clinical.extended.auroc_hidden": 0.973,
    "case.clinical.cbc_iron_crp.ece": 0.08, "case.naive.cbc_iron_crp.ece": 0.23,
    "case.benchmark.extended.acc5_anemic": 0.92, "case.clinical.extended.acc5_anemic": 0.86,
    "case.clinical.cbc.acc5_anemic": 0.66, "case.rows_with_anemia": 531,
    "case.naive.extended.acc12": 0.881, "case.naive.cbc_iron_crp.acc12": 0.571, "case.naive.cbc.acc12": 0.411,
    "case.naive.extended.acc5": 0.94,  # правка 12: «типовой бустинг по 5 группам — 94 %»
    "screen.cohort.rows_total": 88320, "screen.cohort.adults_18plus": 58731, "screen.cohort.with_ferritin": 22752,
    "screen.cohort.train": 16291, "screen.cohort.test": 6461, "screen.cohort.test_normal_hb": 5728,
    "screen.cohort.test_normal_hb_F18_49": 2180,
    "screen.prevalence.F18_49.t15": 0.072, "screen.prevalence.F18_49.t30": 0.276,
    "screen.prevalence.F50.t15": 0.015, "screen.prevalence.F50.t30": 0.078,
    "screen.prevalence.M.t15": 0.006, "screen.prevalence.M.t30": 0.032,
    "screen.no_anemia_share.F18_49.t15": 0.46, "screen.no_anemia_share.F18_49.t30": 0.71,
    "screen.auc.F18_49.t15": 0.82, "screen.auc.F18_49.t15.ci_low": 0.80, "screen.auc.F18_49.t15.ci_high": 0.86,
    "screen.auc.F18_49.t30": 0.73, "screen.auc.F18_49.t30.ci_low": 0.71, "screen.auc.F18_49.t30.ci_high": 0.75,
    "screen.auc_hb_only.F18_49.t15": 0.75, "screen.auc_hb_only.F18_49.t30": 0.66,
    "screen.auc.F50.t15": 0.80, "screen.auc.F50.t30": 0.71, "screen.auc.M.t30": 0.79,
    "screen.auc.ALL.t15": 0.89, "screen.auc.ALL.t30": 0.84,
    "screen.referral.t15.20.sensitivity": 0.67, "screen.referral.t15.20.ppv": 0.24,
    "screen.referral.t15.30.sensitivity": 0.77, "screen.referral.t15.30.ppv": 0.19,
    "screen.referral.t15.40.sensitivity": 0.89, "screen.referral.t15.40.ppv": 0.16,
    "screen.referral.t30.20.sensitivity": 0.42, "screen.referral.t30.20.ppv": 0.58,
    "screen.referral.t30.30.sensitivity": 0.56, "screen.referral.t30.30.ppv": 0.52,
    "screen.referral.t30.40.sensitivity": 0.66, "screen.referral.t30.40.ppv": 0.46,
    "screen.rule.t15.referred_share": 0.15, "screen.rule.t15.rule_sensitivity": 0.51,
    "screen.rule.t15.rule_ppv": 0.25, "screen.rule.t15.model_sensitivity": 0.58, "screen.rule.t15.model_ppv": 0.28,
    "screen.rule.t15.diff_sensitivity_pp": 7, "screen.rule.t15.diff_ci_low_pp": 1, "screen.rule.t15.diff_ci_high_pp": 14,
    "screen.rule.t30.rule_sensitivity": 0.28, "screen.rule.t30.rule_ppv": 0.52,
    "screen.rule.t30.model_sensitivity": 0.33, "screen.rule.t30.model_ppv": 0.61,
    "screen.rule.t30.diff_sensitivity_pp": 5, "screen.rule.t30.diff_ci_low_pp": 2, "screen.rule.t30.diff_ci_high_pp": 8,
    "screen.economics.cases_per1000_t15": 72, "screen.economics.price_ferritin_rub": 845,
    "screen.economics.everyone.rub_per_case": 11775,
    "screen.economics.model_30.found_per1000": 56, "screen.economics.model_30.rub_per_case": 4568,
    "screen.economics.model_20.found_per1000": 48, "screen.economics.model_20.rub_per_case": 3508,
    "screen.economics.rule.referred_per1000": 148, "screen.economics.rule.found_per1000": 37,
    "screen.economics.rule.rub_per_case": 3416,
    "screen.economics.model_at_rule.found_per1000": 42, "screen.economics.model_at_rule.rub_per_case": 3012,
    "screen.case_transfer.auc_raw_t30": 0.68,
    "screen.calibration.F18_49.t30.raw_mean_predicted": 0.42,
    "screen.calibration.F18_49.t30.z_mean_predicted": 0.30, "screen.calibration.F18_49.t30.observed": 0.29,
    "screen.stfr.crp_gt5.30-70": 0.20, "screen.stfr.crp_le5.30-70": 0.06, "screen.stfr.crp_gt5.15-30": 0.37,
    "screen.stfr.crp_le5.15-30": 0.17, "screen.stfr.crp_gt5.100+": 0.10, "screen.stfr.crp_le5.100+": 0.04,
    # прототип мерил только слияние (9 мс на строку; 10 000 строк за 0,64 с) — продукт меряет весь разбор и пакет
    "perf.predict_10000_clinical_s": 0.64,
}

# Числа NUMBERS.md, которых нет в метриках продукта (посчитаны прототипом) — в numbers.json не попадают.
NOT_IN_PRODUCT_METRICS: list[str] = [
    "Наивный бустинг с замком ВОЗ, 12 классов, строка как есть — 0,893",
    "Пороговые правила без ML, строка как есть — 0,755",
    "Мажоритарный класс — 0,137",
    "Модель только на индикаторах «анализ сдан / не сдан» — 0,254",
    "Данные без подсказки: наивный бустинг 0,677; слияние 0,746",
    "Скорость прототипа: 9 мс на строку (только слияние) — в продукте мерится весь разбор (docs/perf.md)",
]


def perf_numbers() -> dict[str, tuple[float, str]]:
    """Несколько чисел из docs/perf.md (файл пишет scripts/perf.py): ключ → (значение, подпись)."""
    if not PERF_MD.is_file():
        return {}
    text = PERF_MD.read_text(encoding="utf-8")

    def num(s: str) -> float:
        return float(s.replace(" ", "").replace(" ", "").replace(",", "."))

    out: dict[str, tuple[float, str]] = {}
    for key, pat, lab in (("perf.analyze.p50_ms", r"^\| p50 \| ([\d,]+) мс", "engine.analyze, p50, мс"),
                          ("perf.analyze.p95_ms", r"^\| p95 \| ([\d,]+) мс", "engine.analyze, p95, мс"),
                          ("perf.models_load_s", r"^\| Загрузка моделей при старте \| ([\d,]+) с",
                           "загрузка моделей при старте, с")):
        m = re.search(pat, text, re.MULTILINE)
        if m:
            out[key] = (num(m.group(1)), lab)
    for size, key in (("10 000", "perf.predict_10000"), ("100 000", "perf.predict_100000")):
        for mode_ru, mode in (("клинический, без меток", "clinical"), ("бенчмарка, без меток", "benchmark")):
            m = re.search(rf"^\| {size} \| {mode_ru} \| ([\d,]+) \|", text, re.MULTILINE)
            if m:
                out[f"{key}_{mode}_s"] = (num(m.group(1)), f"predict_table, {size} строк, режим {mode_ru}, с")
    m = re.search(r"^Замер: (.+?)\.$", text, re.MULTILINE)
    if m:
        out["perf.measured_at"] = (m.group(1), "дата замера")  # type: ignore[assignment]
    return out


def build_numbers(ctx: dict[str, Any]) -> dict[str, Any]:
    """Все числа для слайдов с полем source. Числа по файлу NHANES — только при наличии файла."""
    items: dict[str, dict[str, Any]] = {}

    def put(key: str, value: Any, source: str, label: str, **extra: Any) -> None:
        v = round(float(value), 4) if isinstance(value, (float, np.floating)) else value
        items[key] = {"value": v, "label_ru": label, "source": source, **extra}

    cm, sm = ctx["case"], ctx["screen"]
    src_c = case_src(ctx)
    n_rows = cm["summary"]["n_rows"]
    cv = {"n": n_rows, "cv": cm["summary"]["cv"]}
    put("case.n_rows", n_rows, src_c, CASE_NAME + ": строк")
    put("case.rows_with_anemia", ctx["case_meta"]["summary"]["rows_with_anemia"],
        "models/case-v1/metadata.json (summary)", "Учебный набор кейса: строк с анемией")
    put("case.level1_match", cm["summary"]["level1_match"], src_c,
        "Анемия по ВОЗ совпала с меткой anemia, строк из 840")
    metric_ru = {"acc12": "точность, 12 классов", "f1_12": "macro-F1, 12 классов", "acc5": "точность, 5 групп",
                 "f1_5": "macro-F1, 5 групп", "f1_cause": "macro-F1, причина (как в продукте)",
                 "f1_cause_proto": "macro-F1, причина (как в прототипе)", "acc5_anemic": "точность, 5 групп, строки с анемией",
                 "auroc_hidden": "AUROC скрытого дефицита (строки без анемии)", "ece": "ECE по фолду",
                 "ece_pooled": "ECE по объединённым предсказаниям", "logloss12": "log-loss, 12 классов",
                 "hidden_sensitivity": "флаг скрытого дефицита: чувствительность",
                 "hidden_specificity": "флаг скрытого дефицита: специфичность"}
    mode_ru = {"clinical": "клинический режим", "benchmark": "режим бенчмарка", "naive": "типовой бустинг на всех столбцах"}
    for mode, levels in cm["by_mode_level"].items():
        for lv, lv_ru in zip(LEVELS, LEVEL_RU, strict=True):
            for m, st in levels[lv].items():
                if st["mean"] is None:
                    continue
                put(f"case.{mode}.{lv}.{m}", st["mean"], src_c + ", by_mode_level",
                    f"Кейс, {mode_ru[mode]}, {lv_ru}: {metric_ru.get(m, m)} (среднее по 15 фолдам)",
                    sd=round(st["sd"], 4), **cv)
                if m == "acc12":
                    put(f"case.{mode}.{lv}.acc12_sd", st["sd"], src_c + ", by_mode_level",
                        f"Кейс, {mode_ru[mode]}, {lv_ru}: разброс точности по 15 фолдам (SD)", **cv)
    for mode in ("clinical", "benchmark"):
        g = cm["confusion"][mode]["groups5"]
        mat = np.asarray(g["matrix"], float)
        put(f"case.confusion5.{mode}.first_repeat_acc5_anemic", float(np.trace(mat[1:, 1:]) / mat[1:].sum()),
            src_c + f", confusion.{mode}", f"Кейс, {mode_ru[mode]}, строка как есть: точность по 5 группам "
            "у строк с анемией, первый повтор CV (матрица на слайде)", n=int(mat[1:].sum()))
    put("case.gates.all_passed", bool(cm["summary"]["gates"]["all_passed"]), src_c, "Ворота качества по кейсу пройдены")

    src_s = screen_src(ctx)
    co = sm["cohort"]["counts"]
    for key, k, lab in (("rows_total", "rows_total", "строк в файле NHANES"), ("adults_18plus", "adults_18plus", "взрослых 18+"),
                        ("with_ferritin", "with_ferritin", "небеременных взрослых с полным ОАК и ферритином"),
                        ("train", "with_ferritin_train_1999_2016", "из них в обучении (1999–2016)"),
                        ("test", "with_ferritin_test_2017_2023", "из них в тесте (2017–2023)"),
                        ("test_normal_hb", "no_anemia_test_2017_2023",
                         "тест, без анемии по ВОЗ — на них проверен скрининг (правка 12: 5 728, не 6 461)"),
                        ("train_normal_hb", "no_anemia_train_1999_2016", "обучение, без анемии по ВОЗ")):
        put(f"screen.cohort.{key}", co[k], src_s + ", cohort.counts", f"NHANES: {lab}")
    for g, n in sm["cohort"]["test_no_anemia_by_group"].items():
        put(f"screen.cohort.test_normal_hb_{GKEY[g]}", n, src_s + ", cohort", f"NHANES тест, без анемии, {g}")
    for cyc, v in sm["calibration_shift"]["rdw_median_by_cycle"].items():
        put(f"screen.rdw_median.{cyc}", v, src_s + ", calibration_shift", f"NHANES: медиана RDW, цикл {cyc}, %")
    for g, p in sm["cohort"]["prevalence_no_anemia_test_weighted"].items():
        for t in ("t15", "t30"):
            put(f"screen.prevalence.{GKEY[g]}.{t}", p[f"ferritin_lt{t[1:]}_pct"] / 100, src_s + ", cohort",
                f"NHANES тест, без анемии, {g}: доля ферритина <{t[1:]} (веса NHANES)", n=p["n"])
    for g, p in sm["cohort"]["deficient_without_anemia_test_weighted"].items():
        for t in ("t15", "t30"):
            put(f"screen.no_anemia_share.{GKEY[g]}.{t}", p[f"lt{t[1:]}_without_anemia_pct"] / 100, src_s + ", cohort",
                f"NHANES тест, {g}: доля без анемии среди всех с ферритином <{t[1:]} (веса NHANES)",
                n=p[f"lt{t[1:]}_n"])
    for g, by_t in sm["summary"]["by_group"].items():
        for t, r in by_t.items():
            if r is None:
                continue
            main = " — главная метрика скрининга" if g == "F18-49" else ""
            put(f"screen.auc.{GKEY[g]}.{t}", r["auc"], src_s + ", summary.by_group",
                f"AUC ферритина <{t[1:]}, {g}, модель по ОАК со стандартизацией{main}",
                n=r["n"], cases=r["cases"], ci95=r["ci95"])
            put(f"screen.auc.{GKEY[g]}.{t}.ci_low", r["ci95"][0], src_s, f"нижняя граница 95 % ДИ AUC, {g}, <{t[1:]}")
            put(f"screen.auc.{GKEY[g]}.{t}.ci_high", r["ci95"][1], src_s, f"верхняя граница 95 % ДИ AUC, {g}, <{t[1:]}")
            put(f"screen.calibration_gap.{GKEY[g]}.{t}", r["calibration_gap"], src_s,
                f"средняя предсказанная минус наблюдаемая доля, {g}, <{t[1:]} (без весов)", n=r["n"])
    for t, r in sm["summary"]["hb_only_F18-49"].items():
        put(f"screen.auc_hb_only.F18_49.{t}", r["auc"], src_s + ", summary.hb_only_F18-49",
            f"AUC ферритина <{t[1:]}, Ж 18–49, только Hb + возраст + пол", n=r["n"], cases=r["cases"], ci95=r["ci95"])
    for t, r in sm["summary"]["raw_features_F18-49"].items():
        put(f"screen.auc_raw.F18_49.{t}", r["auc"], src_s + ", summary.raw_features_F18-49",
            f"AUC ферритина <{t[1:]}, Ж 18–49, ОАК без стандартизации", n=r["n"], cases=r["cases"], ci95=r["ci95"])
    nf = sm["referral"]["baselines"]["F18-49"]
    for t in ("t15", "t30"):
        for r in sm["referral"]["tables"]["F18-49"][t]["full_cbc"]:
            s = round(r["refer_share"] * 100)
            for m, m_ru in (("sensitivity", "нашли, доля случаев"), ("ppv", "подтверждается, доля направленных"),
                            ("found_per1000", "найдено на 1000"), ("rub_per_case_found", "₽ на найденный случай")):
                put(f"screen.referral.t{t[1:]}.{s}.{m}", r[m], src_s + ", referral.tables",
                    f"Ж 18–49, <{t[1:]}: направили {s} % → {m_ru} (веса NHANES)", n=nf[t]["n"], cases=nf[t]["cases"])
        rc = sm["rule_comparison"]["by_group"]["F18-49"][t]
        put(f"screen.rule.{t}.referred_share", rc["rule"]["referred_per1000"] / 1000, src_s + ", rule_comparison",
            f"Ж 18–49: доля, которую направляет правило «{sm['rule_comparison']['rule']}»")
        for m, v in (("rule_sensitivity", rc["rule"]["sensitivity"]), ("rule_ppv", rc["rule"]["ppv"]),
                     ("model_sensitivity", rc["model_same_referrals"]["sensitivity"]),
                     ("model_ppv", rc["model_same_referrals"]["ppv"]),
                     ("diff_sensitivity_pp", rc["sensitivity_diff_pp"]),
                     ("diff_ci_low_pp", rc["sensitivity_diff_ci95_pp"][0]),
                     ("diff_ci_high_pp", rc["sensitivity_diff_ci95_pp"][1])):
            put(f"screen.rule.{t}.{m}", v, src_s + ", rule_comparison",
                f"Ж 18–49, <{t[1:]}: модель при тех же направлениях против правила — {m} "
                f"(бутстрэп B={sm['rule_comparison']['bootstrap']['B']})", n=nf[t]["n"], cases=nf[t]["cases"])
    for g, by_t in sm["rule_comparison"]["by_group"].items():
        for t, rc in by_t.items():
            put(f"screen.rule.{GKEY[g]}.{t}.model_better", bool(rc["model_better"]), src_s + ", rule_comparison",
                f"{g}, <{t[1:]}: ДИ разницы чувствительности выше нуля")

    # Экономика на 1000 женщин 18–49 с нормальным Hb, порог ВОЗ <15.
    price = sm["summary"]["price_ferritin_rub"]
    b15 = sm["referral"]["baselines"]["F18-49"]["t15"]
    ref15 = {round(r["refer_share"] * 100): r for r in sm["referral"]["tables"]["F18-49"]["t15"]["full_cbc"]}
    rc15 = sm["rule_comparison"]["by_group"]["F18-49"]["t15"]
    cases = b15["test_everyone"]["cases_per1000"]
    src_e = src_s + ", referral (на 1000 человек, веса NHANES)"
    put("screen.economics.price_ferritin_rub", price, src_s + ", summary", "Цена ферритина, ₽ (медиана Москвы)")
    put("screen.economics.cases_per1000_t15", cases, src_e, "Ж 18–49: случаев ферритина <15 на 1000")
    put("screen.economics.everyone.rub_per_case", b15["test_everyone"]["rub_per_case_found"], src_e,
        "₽ на найденный случай: ферритин всем")
    for s in (20, 30):
        put(f"screen.economics.model_{s}.found_per1000", ref15[s]["found_per1000"], src_e, f"Модель, {s} %: найдено из {cases}")
        put(f"screen.economics.model_{s}.rub_per_case", ref15[s]["rub_per_case_found"], src_e,
            f"₽ на найденный случай: модель, {s} %")
    derived = src_e + "; найдено = sensitivity × cases_per1000, округлено"
    for key, part, lab in (("rule", rc15["rule"], "правило по индексам"),
                           ("model_at_rule", rc15["model_same_referrals"], "модель при тех же направлениях")):
        put(f"screen.economics.{key}.referred_per1000", part["referred_per1000"], src_e, f"{lab}: анализов на 1000")
        put(f"screen.economics.{key}.found_per1000", round(part["sensitivity"] * cases), derived,
            f"{lab}: найдено из {cases}")
        put(f"screen.economics.{key}.rub_per_case", part["rub_per_case_found"], src_e, f"₽ на найденный случай: {lab}")

    for r in sm["calibration_shift"]["rows"]:
        if r["features"] != "full_cbc":
            continue
        base = f"screen.calibration.{GKEY[r['group']]}.{r['target']}"
        lab = f"{r['group']}, <{r['target'][1:]}"
        put(f"{base}.raw_mean_predicted", r["mean_predicted_raw"], src_s + ", calibration_shift",
            f"{lab}: средняя предсказанная, ОАК без стандартизации (без весов)")
        put(f"{base}.z_mean_predicted", r["mean_predicted_z"], src_s + ", calibration_shift",
            f"{lab}: средняя предсказанная после стандартизации (без весов)")
        put(f"{base}.observed", r["observed"], src_s + ", calibration_shift", f"{lab}: наблюдаемая доля (без весов)")
    ct = sm["case_transfer"]
    put("screen.case_transfer.auc_raw_t30", ct["auc_raw_t30"], src_s + ", case_transfer",
        "Модель NHANES на учебном наборе кейса: AUC латентного дефицита против здоровых (<30, сырые)",
        n=ct["n"], cases=ct["cases"])
    put("screen.case_transfer.auc_z_t30", ct["auc_z_default_reference_t30"], src_s + ", case_transfer",
        "То же, стандартизация по справочнику default", n=ct["n"], cases=ct["cases"])
    imp = sm["importance"]["features"]
    put("screen.importance_top", imp[0]["feature"], src_s + ", importance",
        f"Самый важный признак, Ж 18–49, <15 (падение AUC {imp[0]['auc_drop']})")
    put("screen.importance_order", [f["feature"] for f in imp[:5]], src_s + ", importance",
        "Пять самых важных признаков, Ж 18–49, <15")
    put("screen.gates.passed", bool(sm["summary"]["gates"]["passed"]), src_s, "Ворота скрининга пройдены")
    put("screen.prototype_identical", bool(sm["summary"]["prototype_check"]["identical"]), src_s,
        "Метрики скрининга совпали с прототипом")

    src_nh = "scripts/make_figures.py по DL_NHANES_DATA (агрегаты, когорта как в screen.load_cohort)"
    try:
        nh = nhanes(ctx)
        src_nh += f"; SHA-256 файла {'совпадает' if nh['sha_ok'] else 'НЕ совпадает'} с screen_metrics.json"
        h = hidden_by_sex_age(ctx)
        for k, v in h.items():
            if "|" in k:
                sex, band = k.split("|")
                for t in ("t15", "t30"):
                    put(f"screen.hidden.{sex}.{band.replace('–', '-')}.{t}", v[t], src_nh,
                        f"Тест, без анемии, {sex} {band}: доля ферритина <{t[1:]} (веса NHANES)",
                        n=v["n"], cases=v[f"cases_{t}"])
        c = h["check_F18-49"]
        for t in ("t15", "t30"):
            put(f"screen.hidden.check_F18_49.{t}", c[t], src_nh,
                f"Сверка: Ж 18–49, тест, без анемии, доля <{t[1:]} по файлу (должна совпасть с screen.prevalence)",
                n=c["n"])
        s = stfr_by_ferritin(ctx)
        for k, v in s.items():
            if "|" in k:
                infl, lab = k.split("|")
                put(f"screen.stfr.{infl}.{FER_KEYS[lab]}", v["share"], src_nh,
                    f"Доля с повышенным sTfR, {'СРБ >5' if infl == 'crp_gt5' else 'СРБ ≤5'}, ферритин {lab} мкг/л "
                    "(веса NHANES)", n=v["n"])
        put("screen.stfr.share_female", s["share_female"], src_nh, "Доля женщин в выборке sTfR (без весов)", n=s["n"])
    except Skip as e:
        print(f"  numbers.json без пересчёта по файлу NHANES: {e}")
        for k, v in (ctx.get("previous_items") or {}).items():
            if k.startswith(("screen.hidden.", "screen.stfr.")):
                items[k] = v

    for k, (v, lab) in perf_numbers().items():
        put(k, v, "docs/perf.md (scripts/perf.py)", lab)

    cmp_rows = []
    for k, md in NUMBERS_MD.items():
        if k not in items or not isinstance(items[k]["value"], (int, float)) or isinstance(items[k]["value"], bool):
            cmp_rows.append({"key": k, "numbers_md": md, "numbers_json": None, "over_0_02": None,
                             "note": "нет в numbers.json"})
            continue
        v = float(items[k]["value"])
        delta = v - md
        if k.endswith("_pp"):  # п.п. в NUMBERS.md округлены до целых — сравнивается округлённое значение
            kind, big = "pp_rounded", round(v) != md
        elif abs(md) > 1.5:  # рубли, штуки, секунды — относительное расхождение
            kind, big = "relative", abs(delta) / abs(md) > 0.02
        else:
            kind, big = "absolute", abs(delta) > 0.02
        cmp_rows.append({"key": k, "numbers_md": md, "numbers_json": round(v, 4), "delta": round(delta, 4),
                         "kind": kind, "over_0_02": bool(big)})
    return {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "generator": "scripts/make_figures.py",
        "note": ("Числа для слайдов: value + source (+ n, ci95, sd, где есть). Доли — от 0 до 1. NHANES — США; "
                 "учебный набор кейса — 840 строк. Главная метрика скрининга — AUC у женщин 18–49 без анемии "
                 "(screen.auc.F18_49.*), не общий AUC."),
        "sources": {
            "case_metrics": {"path": "docs/metrics/case_metrics.json", "data_sha256": cm["config"]["data_sha256"]},
            "screen_metrics": {"path": "docs/metrics/screen_metrics.json", "generated": sm["generated"],
                               "data_sha256": sm["data"]["sha256"]},
            "case_model": {"path": "models/case-v1/metadata.json", "trained_at": ctx["case_meta"]["trained_at"]},
            "screen_model": {"path": "models/screen-v1/metadata.json", "created": ctx["screen_meta"]["created"]},
        },
        "items": items,
        "comparison_with_NUMBERS_md": cmp_rows,
        "NUMBERS_md_not_in_product_metrics": NOT_IN_PRODUCT_METRICS,
    }


def write_numbers(ctx: dict[str, Any], path: Path = NUMBERS_JSON) -> dict[str, Any]:
    if path.is_file():  # без файла NHANES числа по нему переносятся из прежнего numbers.json
        ctx["previous_items"] = load_json(path).get("items", {})
    data = build_numbers(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    rows = data["comparison_with_NUMBERS_md"]
    big = [r for r in rows if r["over_0_02"]]
    missing = [r for r in rows if r["over_0_02"] is None]
    print(f"numbers.json: {len(data['items'])} чисел → {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"Сверка с NUMBERS.md: {len(rows)} чисел, расхождений >0,02: {len(big)}, нет в numbers.json: {len(missing)}")
    for r in big:
        print(f"  {r['key']}: NUMBERS.md {r['numbers_md']} → {r['numbers_json']} (Δ {r['delta']:+}, {r['kind']})")
    for r in missing:
        print(f"  {r['key']}: нет в numbers.json")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="имена рисунков или numbers")
    ap.add_argument("--out", type=Path, default=FIG_DIR, help="папка рисунков")
    ap.add_argument("--numbers", type=Path, default=NUMBERS_JSON, help="куда писать numbers.json")
    ap.add_argument("--list", action="store_true", help="перечислить рисунки и источники")
    args = ap.parse_args(argv)
    if args.list:
        for name, (_, src) in FIGURES.items():
            print(f"{name:32s} {src}")
        return 0
    only = set(args.only or [*FIGURES, "numbers"])
    unknown = only - set(FIGURES) - {"numbers"}
    if unknown:
        print(f"Неизвестные имена: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    ctx = load_ctx()
    setup_style()
    built, skipped = [], []
    for name, (fn, _) in FIGURES.items():
        if name not in only:
            continue
        t0 = time.perf_counter()
        try:
            paths = fn(args.out, ctx)
            built.append(name)
            print(f"[ok]   {name}: {', '.join(p.name for p in paths)} ({time.perf_counter() - t0:.1f} с)")
        except Skip as e:
            skipped.append(name)
            print(f"[skip] {name}: {e}")
    if "numbers" in only:
        write_numbers(ctx, args.numbers)
    print(f"Построено {len(built)}, пропущено {len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
