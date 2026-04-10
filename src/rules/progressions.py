from __future__ import annotations

import re
from typing import Any, Dict, List

from .dates import DATE_ANY, date_to_iso_like
from .text_utils import norm_spaces

RE_PROGRESSION = re.compile(rf"Прогрессирование\s+от\s+(?P<date>{DATE_ANY})", flags=re.IGNORECASE)


def extract_progressions(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    src = text or ""
    seen = set()
    for m in RE_PROGRESSION.finditer(src):
        d = date_to_iso_like(m.group("date"))
        if d in seen:
            continue
        seen.add(d)
        span = src[max(0, m.start() - 80) : min(len(src), m.end() + 160)]
        out.append({"date": d, "source": norm_spaces(span)})
    return out
