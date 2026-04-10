# src/rules/demographics.py
from __future__ import annotations

import re
from datetime import date
from typing import Optional, Dict, Any

# "Дата рождения 01.01.1970", "Дата рождения: 01.01.1970г", "д/р-01.01.70"
RE_DOB = re.compile(
    r"(?:Дата\s*рождения|дата\s*рожд\.?|д/р|др|DOB)\s*(?:[:\-–—])?\s*"
    # ВАЖНО: сначала 4 цифры года, затем 2; иначе на строке '01.01.1970' матчится '01.01.19'
    # и превращается в 2019-01-01. Также добавляем (?!\d), чтобы не резать 4-значные годы.
    r"(?P<dob>\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2}))(?!\d)\s*(?:г\.?)?",
    flags=re.IGNORECASE,
)

def ddmmyyyy_to_iso(s: str) -> Optional[str]:
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})", (s or "").strip())
    if not m:
        return None
    dd, mm, yy = m.groups()
    dd_i, mm_i = int(dd), int(mm)
    yyyy_i = int(yy)
    if len(yy) == 2:
        # грубая эвристика: 00–30 -> 2000+, иначе 1900+
        yyyy_i = (2000 + yyyy_i) if yyyy_i <= 30 else (1900 + yyyy_i)
    try:
        return date(yyyy_i, mm_i, dd_i).isoformat()
    except ValueError:
        return None

def extract_dob(text: str) -> Optional[str]:
    head = (text or "")[:8000]
    m = RE_DOB.search(head)
    return ddmmyyyy_to_iso(m.group("dob")) if m else None

def infer_sex(text: str) -> Optional[str]:
    """Определяем пол ТОЛЬКО по явному указанию в тексте.

    Важно для анти-галлюцинаций: слова «пациент/пациентка» не считаем
    достаточным основанием для заполнения sex.
    """
    head = (text or "")[:20000]

    # варианты: "пол: жен", "пол - мужской", "Пол женский"
    m = re.search(r"\bпол\s*(?:[:\-–—])?\s*(жен\w*|муж\w*)\b", head, flags=re.IGNORECASE)
    if m:
        g = m.group(1).lower()
        return "F" if g.startswith("жен") else "M"

    # иногда в шапке пишут "Пол Ж" / "Пол: М"
    m2 = re.search(r"\bпол\s*(?:[:\-–—])?\s*([ЖМ])\b", head, flags=re.IGNORECASE)
    if m2:
        return "F" if m2.group(1).upper() == "Ж" else "M"

    return None

def fill_demographics_inplace(data: Dict[str, Any], *, text: str) -> None:
    """Заполняет patient.demographics.dob/sex, если они пустые."""
    if not isinstance(data, dict):
        return
    patient = data.get("patient")
    if not isinstance(patient, dict):
        return
    demo = patient.get("demographics")
    if not isinstance(demo, dict):
        return

    if not demo.get("dob"):
        demo["dob"] = extract_dob(text)
    if not demo.get("sex"):
        demo["sex"] = infer_sex(text)