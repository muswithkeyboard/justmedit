"""Контракты входа и выхода движка (ЗАМОРОЖЕНЫ: можно только добавлять необязательные поля).

AnalysisInput  — один анализ: пол, возраст, беременность, значения показателей.
AnalysisResult — полный ответ: уровень 1 (анемия по ВОЗ), уровень 2 (группа, класс, причина),
                 скрытый дефицит, флаги-подсказки, следующий анализ, объяснение, таблица значений,
                 отчёты врачу и пациенту.
Сервис ничего не хранит: эти объекты живут только в памяти запроса.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

REFER_SHARE_OPTIONS = (0.2, 0.3, 0.4)     # переключатель точки направления на ферритин (доли группы)


# --------------------------------------------------------------------------------------------
# Вход
# --------------------------------------------------------------------------------------------
class Pregnancy(BaseModel):
    status: Literal["no", "yes", "unknown"] = "no"
    trimester: Optional[Literal[1, 2, 3]] = None


class LabValue(BaseModel):
    value: float
    unit: Optional[str] = None  # если не указана — принимается каноническая единица из config/analytes.yaml


class ReferenceRangeInput(BaseModel):
    """Референс лаборатории для одного показателя (с бланка). Хотя бы одна граница; единица — любая допустимая
    для показателя по config/analytes.yaml (не указана — каноническая)."""
    model_config = ConfigDict(extra="forbid")

    low: Optional[float] = None
    high: Optional[float] = None
    unit: Optional[str] = None


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sex: Literal["F", "M"]
    age_years: int = Field(ge=18, le=120, description="Только взрослые: модель и пороги не рассчитаны на детей")
    pregnancy: Pregnancy = Field(default_factory=Pregnancy)
    values: dict[str, Union[LabValue, float]] = Field(description="Код показателя -> значение (и единица)")
    norms: Literal["who", "ru"] = "who"                       # какой набор порогов показывать первым
    mode: Literal["clinical", "benchmark", "auto"] = "clinical"
    lab_reference: str = "default"                             # справочник ОАК лаборатории для скрининга
    # Точка направления на ферритин по скринингу ОАК: доля проверенной группы с наибольшей вероятностью
    # (0.2 / 0.3 / 0.4); None — значение из config/screen_thresholds.yaml (refer_share, сейчас 0.3).
    refer_share: Optional[float] = None
    patient_ref: Optional[str] = None                          # метка строки (например, patient_id файла); не ПДн
    provenance: Optional[dict[str, Any]] = None                # источник ввода: manual / file / api
    # Референсы лаборатории (код показателя -> границы): заменяют референсы проекта по умолчанию для этих
    # показателей; в ответе тогда предупреждение (references.warning). None — только конфигурация.
    reference_ranges: Optional[dict[str, ReferenceRangeInput]] = None

    @field_validator("refer_share")
    @classmethod
    def _refer_share_option(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        for option in REFER_SHARE_OPTIONS:
            if abs(float(v) - option) < 1e-9:
                return option
        raise ValueError("refer_share: 0.2, 0.3 или 0.4")


# --------------------------------------------------------------------------------------------
# Выход
# --------------------------------------------------------------------------------------------
class NormalizationNote(BaseModel):
    analyte: str
    kind: Literal["converted", "unit_assumed", "soft_range", "derived", "ignored"]
    text: str
    original: Optional[float] = None
    value: Optional[float] = None
    from_unit: Optional[str] = None
    to_unit: Optional[str] = None


class UrgentItem(BaseModel):
    code: str
    text: str
    source: str


class Level1(BaseModel):
    anemia: bool
    hemoglobin: float
    threshold_g_l: float
    severity: Optional[Literal["mild", "moderate", "severe"]] = None
    severity_ru: Optional[str] = None
    source: str
    note: Optional[str] = None
    urgent: list[UrgentItem] = Field(default_factory=list)


class ClassProb(BaseModel):
    code: str
    name_ru: str
    p: float


class Level2(BaseModel):
    enabled: bool
    disabled_reason: Optional[str] = None
    mode: Optional[Literal["clinical", "benchmark"]] = None
    completeness: Optional[Literal["cbc", "cbc_iron_crp", "standard", "extended"]] = None
    completeness_ru: Optional[str] = None
    case_group: Optional[str] = None
    case_group_ru: Optional[str] = None
    anemia_class: Optional[str] = None
    anemia_class_ru: Optional[str] = None
    deficiency_cause: Optional[str] = None
    deficiency_cause_ru: Optional[str] = None
    probabilities: dict[str, dict[str, float]] = Field(default_factory=dict)  # classes12 / groups5 / causes11
    plausible: list[ClassProb] = Field(default_factory=list)
    ambiguous: bool = False
    confidence: Optional[Literal["low", "moderate", "high"]] = None
    confidence_ru: Optional[str] = None
    # Строка к классу для врача: «Возможна железодефицитная анемия на фоне воспаления» (СРБ выше порога и признаки
    # дефицита железа; решение капитана 04.10.2026, п. 1). None — строки нет.
    class_note_ru: Optional[str] = None
    # Пояснение к группе кейса: анемия воспаления в пяти группах кейса относится к «анемии неясного генеза».
    case_group_note_ru: Optional[str] = None


class RuleSignal(BaseModel):
    code: str
    text: str
    source: str


class Screening(BaseModel):
    """Скрининг скрытого дефицита железа по ОАК (модель на реальных людях, NHANES)."""
    applicable: bool
    reason_not_applicable: Optional[str] = None
    model: Optional[str] = None
    p_ferritin_lt15: Optional[float] = None
    p_ferritin_lt30: Optional[float] = None
    risk: Optional[Literal["usual", "elevated", "high"]] = None
    risk_ru: Optional[str] = None
    explanation_ru: Optional[str] = None
    features: Optional[Literal["full_cbc", "hb_only"]] = None
    group: Optional[str] = None
    validated_group: Optional[bool] = None
    lab_reference: Optional[str] = None
    recommend_ferritin: bool = False
    refer_share: Optional[float] = None   # применённая точка направления на ферритин (доля группы)


class HiddenDeficiency(BaseModel):
    applicable: bool                      # только при отсутствии анемии
    detected: bool = False                # есть ли сигнал скрытого дефицита (правило, модель кейса или скрининг)
    p_any: Optional[float] = None         # модель кейса: P(любой дефицит | нет анемии); «по данным кейса (учебный набор кейса, 840 строк)»
    by_nutrient: dict[str, float] = Field(default_factory=dict)
    rule_signals: list[RuleSignal] = Field(default_factory=list)
    screening: Optional[Screening] = None
    summary_ru: str = ""


class Flag(BaseModel):
    code: str
    title: str
    text: str
    source: str
    severity: Literal["info", "attention"] = "attention"


class ChecklistItem(BaseModel):
    code: str
    title: str
    status: Literal["excluded", "suspected", "not_checked"]
    detail: str
    analyte: Optional[str] = None         # показатель, по которому оценён пункт, если он не основной для пункта


class NextTest(BaseModel):
    analyte: str
    name_ru: str
    reason: str
    kind: Literal["rule", "information"]
    information_gain: Optional[float] = None
    price_rub: Optional[int] = None
    available: bool = True
    source: Optional[str] = None


class Contribution(BaseModel):
    analyte: str
    name_ru: str
    value: float
    unit: str
    direction: Literal["for", "against"]
    weight: float
    text: str


class Explanation(BaseModel):
    top_class: Optional[str] = None
    runner_up: Optional[str] = None
    items_for: list[Contribution] = Field(default_factory=list)
    items_against: list[Contribution] = Field(default_factory=list)
    note: Optional[str] = None


class NormInfo(BaseModel):
    status: Literal["low", "normal", "high", "borderline", "unknown"]
    threshold_text: str = ""
    source: str = ""


class ValueRow(BaseModel):
    analyte: str
    name_ru: str
    value: float
    unit: str
    norm: NormInfo
    contribution: Optional[str] = None
    warning: Optional[str] = None


class ReportSection(BaseModel):
    title: str
    lines: list[str]


class Report(BaseModel):
    headline: str
    sections: list[ReportSection]


class Reports(BaseModel):
    doctor: Report
    patient: Report


class VersionInfo(BaseModel):
    engine: str
    case_model: str
    screen_model: str
    norms: str


class ReferencesInfo(BaseModel):
    """Какие референсы применены: проекта по умолчанию или (частично) лаборатории."""
    set: Literal["default", "lab"] = "default"
    lab_name: Optional[str] = None                 # имя набора из config или "request"
    local_analytes: list[str] = Field(default_factory=list)
    warning: Optional[str] = None                  # «Использованы референсные интервалы лаборатории…» (texts_ru/doctor.yaml)


class AnalysisResult(BaseModel):
    version: VersionInfo
    patient_ref: Optional[str] = None
    level1: Level1
    level2: Level2
    hidden_deficiency: HiddenDeficiency
    flags: list[Flag] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    next_tests: list[NextTest] = Field(default_factory=list)
    explanation: Explanation = Field(default_factory=Explanation)
    values: list[ValueRow] = Field(default_factory=list)
    reports: Reports
    normalization: list[NormalizationNote] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    processing_ms: float = 0.0
    references: Optional[ReferencesInfo] = None


# --------------------------------------------------------------------------------------------
# Ошибки и пакетная обработка
# --------------------------------------------------------------------------------------------
class ErrorBody(BaseModel):
    code: str
    message: str
    field: Optional[str] = None


class InputError(Exception):
    """Ошибка входных данных: превращается в ответ 422 {"error": {code, message, field}}."""

    def __init__(self, code: str, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "field": self.field}}


class BatchItem(BaseModel):
    index: int
    patient_ref: Optional[str] = None
    status: Literal["done", "rejected"]
    errors: list[ErrorBody] = Field(default_factory=list)
    result: Optional[AnalysisResult] = None


class BatchSummary(BaseModel):
    total: int
    done: int
    rejected: int
    processing_ms: float
