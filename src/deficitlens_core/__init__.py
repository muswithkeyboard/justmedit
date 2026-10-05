"""DeficitLens — движок: анемия по ВОЗ, группа и причина, скрытые дефициты, следующий анализ, отчёты.

Исследовательский прототип хакатона MedITron 2026. Не медицинское изделие; решение принимает врач.

Пакет при импорте ничего тяжёлого не загружает: __version__ берётся из constants (там numpy) только при обращении.
Иначе каждый дочерний процесс разбора PDF и фото (`python -m deficitlens_core.io.pdf_text`) тратил бы ~0,2 с
на импорт numpy, который ему не нужен.
"""


def __getattr__(name: str):
    if name == "__version__":
        from .constants import ENGINE_VERSION

        return ENGINE_VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
