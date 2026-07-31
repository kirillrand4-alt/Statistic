"""Правила отбора фактов для базы центробежных воздушных компрессоров."""
from __future__ import annotations

import re

# Любое явное упоминание воздуха важнее других слов: записи вроде
# «газ и воздух» или «газовоздушная смесь» сохраняются.
_AIR_RE = re.compile(r"(?:воздух\w*|воздуш\w*|\bair\b)", re.I)

# Пустые и неопределённые значения не считаются доказательством нецелевой среды.
_UNKNOWN_RE = re.compile(
    r"(?:неизвест|не\s*указ|не\s*определ|нет\s*данн|данн\w*\s*нет|"
    r"информац\w*\s*нет|^\s*[-—–]+\s*$)",
    re.I,
)

# Отсекаются только явно не воздушные среды. Запись всё равно сохраняется,
# когда одновременно найден воздух: «газ и воздух» не удаляется.
_NON_AIR_RE = re.compile(
    r"(?:\bгаз\w*|азот\w*|кислород\w*|водород\w*|метан\w*|пропан\w*|"
    r"бутан\w*|этилен\w*|ацетилен\w*|аммиак\w*|аргон\w*|гелий\w*|"
    r"неон\w*|фреон\w*|хладагент\w*|сероводород\w*|углекисл\w*|"
    r"диоксид\s+углерод\w*|\bco2\b|хлор\w*|водян\w*\s+пар\w*|"
    r"\bпар\w*|\bвод\w*|жидк\w*|нефт\w*|масл\w*)",
    re.I,
)

# Насосы не относятся к базе компрессоров. Проверяем поля, которые описывают
# сам объект, а не длинную цитату: слово «насос» в стороннем контексте цитаты
# не должно случайно скрыть факт о компрессоре.
_PUMP_RE = re.compile(r"насос\w*", re.I)


def is_definitely_non_air_medium(value: object) -> bool:
    """True только для явно не воздушной среды без упоминания воздуха."""
    text = " ".join(str(value or "").strip().split())
    if not text:
        return False
    if _AIR_RE.search(text):
        return False
    if _UNKNOWN_RE.search(text):
        return False
    return bool(_NON_AIR_RE.search(text))


def is_pump_fact(*, status: object = "", model: object = "", equipment_type: object = "") -> bool:
    """True, когда идентифицирующие поля явно описывают насос."""
    identity = " | ".join(str(value or "") for value in (status, model, equipment_type))
    return bool(_PUMP_RE.search(identity))


def keep_fact(fact: dict) -> bool:
    """Оставить воздушный/неопределённый компрессорный факт в интерфейсе."""
    if is_definitely_non_air_medium(fact.get("medium")):
        return False
    if is_pump_fact(
        status=fact.get("status"),
        model=fact.get("model"),
        equipment_type=fact.get("equipment_type"),
    ):
        return False
    return True
