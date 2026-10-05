"""Загрузка конфигурации из каталога config/ (все пороги, названия и цены — только там, не в коде)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return Path(os.environ.get("DL_CONFIG_DIR", REPO_ROOT / "config"))


def models_dir() -> Path:
    return Path(os.environ.get("DL_MODELS_DIR", REPO_ROOT / "models"))


def demo_dir() -> Path:
    return Path(os.environ.get("DL_DEMO_DIR", REPO_ROOT / "data" / "demo"))


def _yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class Config:
    analytes: dict[str, dict]            # код -> описание показателя
    norms: dict[str, Any]                # пороги показа и правил с источниками
    class_mapping: dict[str, Any]
    prices: dict[str, dict]
    lab_reference: dict[str, Any]        # справочники ОАК лабораторий для скрининга
    screen: dict[str, Any]               # рабочая точка скрининга
    texts: dict[str, Any] = field(default_factory=dict)          # шаблоны текстов (config/texts_ru/*.yaml)
    forbidden_patient_words: tuple[str, ...] = ()

    def name_ru(self, analyte: str) -> str:
        return self.analytes.get(analyte, {}).get("name_ru", analyte)

    def short_ru(self, analyte: str) -> str:
        return self.analytes.get(analyte, {}).get("short_ru", analyte)

    def unit_ru(self, analyte: str) -> str:
        return self.analytes.get(analyte, {}).get("unit_ru", "")

    def price(self, analyte: str) -> tuple[int | None, bool]:
        p = self.prices.get(analyte, {})
        return p.get("price"), bool(p.get("available", True))

    def source_title(self, key: str) -> str:
        return self.norms.get("sources", {}).get(key, {}).get("title", key)


@lru_cache(maxsize=4)
def load_config(directory: str | None = None) -> Config:
    d = Path(directory) if directory else config_dir()
    texts: dict[str, Any] = {}
    tdir = d / "texts_ru"
    if tdir.is_dir():
        for p in sorted(tdir.glob("*.yaml")):
            texts[p.stem] = _yaml(p)
    forbidden = _yaml(d / "forbidden_patient_words.yaml") if (d / "forbidden_patient_words.yaml").exists() else {}
    return Config(
        analytes=_yaml(d / "analytes.yaml")["analytes"],
        norms=_yaml(d / "norms_ru.yaml"),
        class_mapping=_yaml(d / "class_mapping.yaml"),
        prices=_yaml(d / "prices.yaml")["prices"],
        lab_reference=_yaml(d / "lab_reference.yaml"),
        screen=_yaml(d / "screen_thresholds.yaml"),
        texts=texts,
        forbidden_patient_words=tuple((forbidden or {}).get("words", [])),
    )
