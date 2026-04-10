# src/rules_to_case.py
"""Backwards-compatible фасад.

Раньше вся логика правил лежала в одном файле. Теперь она разнесена по модулям
в пакете `src.rules.*`, но этот файл оставлен, чтобы не ломать импорты.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from .rules import (  # re-export
    Biomarker,
    TherapyLine,
    build_case_from_rules,
    extract_nosology,
    extract_progressions,
    extract_therapy_lines,
    extract_tnm,
    load_json,
    write_json,
)
from .rules.biomarkers import extract_biomarkers as _extract_biomarkers


def extract_biomarkers(text: str, *, profile: str = "unknown") -> List[Biomarker]:
    """Совместимость: ранее был параметр profile (не использовался)."""
    _ = profile
    return _extract_biomarkers(text)


__all__ = [
    "Biomarker",
    "TherapyLine",
    "build_case_from_rules",
    "extract_biomarkers",
    "extract_therapy_lines",
    "extract_tnm",
    "extract_nosology",
    "extract_progressions",
    "load_json",
    "write_json",
]


if __name__ == "__main__":
    TEMPLATE_PATH = Path("examples/case_empty.json")
    OUT_PATH = Path("data/outputs/case_0001/case.json")
    TEXT_PATH = Path("data/outputs/case_0001/focus.txt")

    template = load_json(TEMPLATE_PATH)
    text = TEXT_PATH.read_text(encoding="utf-8")

    case = build_case_from_rules(text=text, template=template, case_id="case_0001")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(OUT_PATH, case)
    print("OK:", OUT_PATH)
