"""Общие константы движка: порядок признаков, группы анализов, профили, классы и свёртки.

Источник структуры — файл кейса Сеченовского университета (840 строк, 48 столбцов):
12 значений anemia_class, 11 значений deficiency_cause; смешанный дефицит разбит на 3 подтипа,
поэтому модель работает с 14 «профилями» и сворачивает их в классы, причины и 5 групп кейса.
Канонические ключи показателей = имена столбцов файла кейса.
"""
from __future__ import annotations

import numpy as np

ENGINE_VERSION = "0.1.0"

# --- Признаки -------------------------------------------------------------------------------
# Общий анализ крови (ОАК) + возраст и пол: заполнены у всех, по ним строится приор.
CBC = ["age_years", "sex", "hemoglobin", "RBC", "hematocrit", "MCV", "MCH", "MCHC", "RDW", "platelets", "WBC"]
# Показатели ОАК без демографии (для скрининга и стандартизации по лаборатории).
CBC_LABS = ["hemoglobin", "RBC", "hematocrit", "MCV", "MCH", "MCHC", "RDW", "platelets", "WBC"]

# Группы связанных анализов вне ОАК. Внутри группы анализы коррелируют, поэтому вес задаётся на группу.
GROUPS: dict[str, list[str]] = {
    "iron": ["ferritin", "serum_iron", "transferrin", "TIBC", "UIBC", "TSAT", "sTfR", "Ret_He"],
    "b12": ["vitamin_B12", "active_B12", "MMA", "homocysteine"],
    "folate": ["folate"],
    "copper": ["copper", "ceruloplasmin"],
    "b6": ["vitamin_B6"],
    "inflammation": ["CRP", "ESR"],
    "hemolysis": ["LDH", "indirect_bilirubin", "haptoglobin", "reticulocytes"],
    "kidney": ["creatinine", "eGFR"],
    "tsh": ["TSH"],
    "albumin": ["albumin"],
}
GROUP_NAMES = list(GROUPS)
LABS = [a for g in GROUPS.values() for a in g]  # 26 анализов вне ОАК
FEATS = CBC + LABS  # 37 признаков — порядок столбцов матрицы X
F_IDX = {f: i for i, f in enumerate(FEATS)}
CBC_IDX = [F_IDX[f] for f in CBC]
LAB_IDX = [F_IDX[f] for f in LABS]
LAB_GROUP = [GROUP_NAMES.index(g) for g in GROUP_NAMES for _ in GROUPS[g]]  # номер группы каждого анализа
ANALYTE_GROUP = {a: g for g, items in GROUPS.items() for a in items}
ALL_ANALYTES = CBC_LABS + LABS  # всё, что может прийти в values

# Уровни полноты панели (что, кроме ОАК, входит в уровень).
_L_IRON = ["ferritin", "serum_iron", "transferrin", "TIBC", "UIBC", "TSAT", "CRP"]
LEVELS: dict[str, list[str]] = {
    "cbc": [],
    "cbc_iron_crp": _L_IRON,
    "standard": _L_IRON + ["vitamin_B12", "folate"],
    "extended": LABS,
}

# --- Профили, классы, причины, группы --------------------------------------------------------
PROFILES = [
    "no_anemia_no_deficiency", "latent_deficiency", "B12_deficiency_no_anemia", "folate_deficiency_no_anemia",
    "iron_deficiency_anemia", "B12_deficiency_anemia", "folate_deficiency_anemia",
    "mixed:iron_B12", "mixed:iron_folate", "mixed:B12_folate",
    "inflammation_anemia", "anemia_other", "B6_deficiency", "copper_deficiency",
]
NP_ = len(PROFILES)
P_IDX = {p: i for i, p in enumerate(PROFILES)}
NONANEMIC = [0, 1, 2, 3]             # профили, возможные только без анемии
ANEMIC = [4, 5, 6, 7, 8, 9, 10, 11]  # только при анемии
BOTH = [12, 13]                      # B6 и медь встречаются и с анемией, и без неё
ALLOW = np.zeros((2, NP_), dtype=bool)  # «замок ВОЗ»: ALLOW[анемия по Hb] — какие профили допустимы
ALLOW[0, NONANEMIC + BOTH] = True
ALLOW[1, ANEMIC + BOTH] = True

CLASSES12 = [
    "no_anemia_no_deficiency", "latent_deficiency", "B12_deficiency_no_anemia", "folate_deficiency_no_anemia",
    "iron_deficiency_anemia", "B12_deficiency_anemia", "folate_deficiency_anemia", "mixed_deficiency",
    "inflammation_anemia", "anemia_other", "B6_deficiency", "copper_deficiency",
]
CAUSES11 = ["none", "iron_deficiency", "B12_deficiency", "folate_deficiency", "iron_B12", "iron_folate",
            "B12_folate", "inflammation", "undetermined", "B6_deficiency", "copper_deficiency"]
PROFILE_TO_CLASS = {p: ("mixed_deficiency" if p.startswith("mixed:") else p) for p in PROFILES}
PROFILE_TO_CAUSE = {
    "no_anemia_no_deficiency": "none", "latent_deficiency": "iron_deficiency",
    "B12_deficiency_no_anemia": "B12_deficiency", "folate_deficiency_no_anemia": "folate_deficiency",
    "iron_deficiency_anemia": "iron_deficiency", "B12_deficiency_anemia": "B12_deficiency",
    "folate_deficiency_anemia": "folate_deficiency", "mixed:iron_B12": "iron_B12",
    "mixed:iron_folate": "iron_folate", "mixed:B12_folate": "B12_folate",
    "inflammation_anemia": "inflammation", "anemia_other": "undetermined",
    "B6_deficiency": "B6_deficiency", "copper_deficiency": "copper_deficiency",
}
# Матрицы свёртки вероятностей: 14 профилей -> 12 классов / 11 причин.
A12 = np.zeros((NP_, len(CLASSES12)))
A11 = np.zeros((NP_, len(CAUSES11)))
for _p, _i in P_IDX.items():
    A12[_i, CLASSES12.index(PROFILE_TO_CLASS[_p])] = 1
    A11[_i, CAUSES11.index(PROFILE_TO_CAUSE[_p])] = 1

GROUPS5 = ["healthy", "iron_deficiency_anemia", "vitamin_B12_deficiency_anemia", "unexplained_anemia",
           "other_deficiency_anemia"]
# Группа кейса для каждого из 12 классов ПРИ НАЛИЧИИ анемии (без анемии группа всегда healthy).
# Порядок соответствует CLASSES12. Для «безанемичных» классов задано ближайшее по смыслу значение —
# оно используется, только если модель выбрала такой класс при анемии по Hb (после «замка ВОЗ» это невозможно).
G5_IF_ANEMIA = np.array([3, 1, 2, 4, 1, 2, 4, 4, 3, 3, 4, 4])

# Какой нутриент затронут в каждом профиле (для разбивки скрытого дефицита по нутриентам).
PROFILE_NUTRIENTS: dict[str, list[str]] = {
    "no_anemia_no_deficiency": [], "latent_deficiency": ["iron"], "B12_deficiency_no_anemia": ["b12"],
    "folate_deficiency_no_anemia": ["folate"], "iron_deficiency_anemia": ["iron"], "B12_deficiency_anemia": ["b12"],
    "folate_deficiency_anemia": ["folate"], "mixed:iron_B12": ["iron", "b12"], "mixed:iron_folate": ["iron", "folate"],
    "mixed:B12_folate": ["b12", "folate"], "inflammation_anemia": [], "anemia_other": [],
    "B6_deficiency": ["b6"], "copper_deficiency": ["copper"],
}
NUTRIENTS = ["iron", "b12", "folate", "b6", "copper"]


def group5(cls12, anemia):
    """Группа кейса по индексу класса (0..11) и анемии по ВОЗ (0/1). Работает с массивами numpy."""
    return np.where(np.asarray(anemia) == 1, G5_IF_ANEMIA[np.asarray(cls12)], 0)
