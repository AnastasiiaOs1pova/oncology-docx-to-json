from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


RE_DIAG_LINE = re.compile(
    r"(?im)^\s*(?:диагноз|ds)\b[^\n]{0,600}",
)

# стадия: "стадия IV" / "IIA ст." / "4 стадия"
RE_STAGE = re.compile(
    r"(?i)\b(?:стадия\s*(?P<s1>(?:[IVX]{1,4}|[0-4])(?:[ABC])?)\b|"
    r"(?P<s2>(?:[IVX]{1,4}|[0-4])(?:[ABC])?)\s*ст\.?\b)"
)

RE_TRIPLE_NEG = re.compile(r"(?i)\bтрижды\s+негативн\w+\b")


def _pick_diag_segment(text: str) -> str:
    """Берём короткий сегмент, где обычно находится стадия.

    Это защищает от ложных совпадений типа "анемия 2 ст.".
    """
    t = text or ""
    m = RE_DIAG_LINE.search(t)
    if m:
        return (m.group(0) or "").strip()
    # fallback: первые 800 символов (обычно там есть строка диагноза)
    return t[:800]


def extract_primary_diagnosis(text: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """Минимальный агрегатор диагноза для MVP.

    Цель: заполнить stage/subtype только если это явно присутствует в тексте.
    Никаких клинических выводов и нормализации.
    """
    seg = _pick_diag_segment(text)
    diag: Dict[str, Any] = {
        "disease": None,
        "subtype": None,
        "icd10": None,
        "morphology": None,
        "stage": None,
        "tnm": {},
    }

    # стадия (строго из сегмента диагноза)
    sm = RE_STAGE.search(seg)
    if sm:
        st = (sm.group("s1") or sm.group("s2") or "").strip()
        if st:
            # верхний регистр для римских + A/B/C
            diag["stage"] = st.upper()

    # подтип (минимально: triple-negative)
    tm = RE_TRIPLE_NEG.search(seg)
    if tm:
        diag["subtype"] = tm.group(0).strip()

    # profile пока не меняем: пусть остаётся из носологии
    return diag, None
