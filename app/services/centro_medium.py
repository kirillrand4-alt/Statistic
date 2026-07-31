"""Правила отбора фактов для базы центробежных воздушных компрессоров."""
from __future__ import annotations

import re

# Любое явное упоминание воздуха важнее других слов: записи вроде
# «газ и воздух» или «газовоздушная смесь» сохраняются.
_AIR_RE = re.compile(r"(?:воздух\w*|воздуш\w*|\bair\b)", re.I)

# Пустые и неопределённые значения не считаются доказательством нецелевой среды.
_UNKNOWN_RE = re.compile(
    r"(?:неизвест|не\s*указ|не\s*назван|не\s*определ|нет\s*данн|"
    r"данн\w*\s*нет|информац\w*\s*нет|среда\s+не\s+названа|"
    r"^\s*[-—–]+\s*$)",
    re.I,
)

# Отсекаются только явно не воздушные среды. Запись всё равно сохраняется,
# когда одновременно найден воздух.
_NON_AIR_RE = re.compile(
    r"(?:\bгаз\w*|азот\w*|кислород\w*|водород\w*|метан\w*|пропан\w*|"
    r"бутан\w*|этилен\w*|ацетилен\w*|аммиак\w*|аргон\w*|гелий\w*|"
    r"неон\w*|фреон\w*|хладагент\w*|сероводород\w*|углекисл\w*|"
    r"диоксид\s+углерод\w*|\bco2\b|хлор\w*|водян\w*\s+пар\w*|"
    r"\bпар\w*|\bвод\w*|жидк\w*|нефт\w*)",
    re.I,
)

# Выявление газового компрессора в тексте нужно для строк, где поле среды
# пустое или нормализовано как «среда не названа».
_GAS_COMPRESSOR_RE = re.compile(
    r"(?:газов\w*\s+компрессор\w*|компрессор\w*.{0,80}(?:природн\w*\s+газ|"
    r"метан\w*|пропан\w*|бутан\w*|водород\w*|азот\w*|кислород\w*))",
    re.I | re.S,
)

# Насосы не относятся к базе компрессоров. Сильным признаком считается слово
# «насос» в модели/типе/статусе. В длинном тексте оно исключает строку только
# тогда, когда там нет одновременного упоминания компрессора.
_PUMP_RE = re.compile(r"насос\w*", re.I)
_COMPRESSOR_RE = re.compile(r"(?:компресс\w*|турбокомпресс\w*|нагнетател\w*)", re.I)


def _text(*values: object) -> str:
    return " | ".join(" ".join(str(value or "").strip().split()) for value in values)


def has_air(*values: object) -> bool:
    """Есть ли явное упоминание воздуха хотя бы в одном поле."""
    return bool(_AIR_RE.search(_text(*values)))


def is_definitely_non_air_medium(value: object, *, context: object = "") -> bool:
    """True только для явно не воздушной среды без упоминания воздуха.

    ``context`` позволяет сохранить строку «газ и воздух», даже когда поле
    ``sreda`` было предварительно сведено к категории «газ или иная среда».
    """
    medium = _text(value)
    combined = _text(value, context)
    if not medium:
        return False
    if _AIR_RE.search(combined):
        return False
    if _UNKNOWN_RE.search(medium):
        return False
    return bool(_NON_AIR_RE.search(medium))


def is_pump_fact(
    *,
    status: object = "",
    model: object = "",
    equipment_type: object = "",
    evidence: object = "",
    quote: object = "",
) -> bool:
    """True, когда строка описывает насос, а не компрессор."""
    identity = _text(status, model, equipment_type)
    if _PUMP_RE.search(identity):
        return True

    context = _text(evidence, quote)
    return bool(_PUMP_RE.search(context) and not _COMPRESSOR_RE.search(context))


def rejection_reason(fact: dict) -> str:
    """Вернуть причину исключения или пустую строку для сохраняемого факта."""
    if is_pump_fact(
        status=fact.get("status"),
        model=fact.get("model"),
        equipment_type=fact.get("equipment_type"),
        evidence=fact.get("evidence"),
        quote=fact.get("quote"),
    ):
        return "pump"

    context = _text(
        fact.get("status"),
        fact.get("model"),
        fact.get("equipment_type"),
        fact.get("evidence"),
        fact.get("quote"),
    )
    if is_definitely_non_air_medium(fact.get("medium"), context=context):
        return "non_air"

    # При неопределённом поле среды исключаем только явную конструкцию
    # «газовый компрессор» или компрессор конкретного газа.
    combined = _text(fact.get("medium"), context)
    if not _AIR_RE.search(combined) and _GAS_COMPRESSOR_RE.search(combined):
        return "non_air"

    return ""


def keep_fact(fact: dict) -> bool:
    """Оставить воздушный или неопределённый компрессорный факт."""
    return not rejection_reason(fact)
