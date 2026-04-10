from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Dict, List

from .paths import BIOMARKERS_CATALOG_PATH


@lru_cache(maxsize=1)
def load_biomarkers_catalog_compiled() -> List[Dict[str, Any]]:
    """Компилируем biomarkers.json в удобную для правил структуру."""
    if not BIOMARKERS_CATALOG_PATH.exists():
        raise FileNotFoundError(f"Не найден biomarkers.json: {BIOMARKERS_CATALOG_PATH}")

    data = json.loads(BIOMARKERS_CATALOG_PATH.read_text(encoding="utf-8"))
    items = data.get("biomarkers", []) if isinstance(data, dict) else []
    out: List[Dict[str, Any]] = []

    for b in items:
        if not isinstance(b, dict):
            continue
        canon = (b.get("canon") or "").strip()
        if not canon:
            continue

        std = (b.get("std") or canon).strip().lower()
        vtype = (b.get("value_type") or "none").strip().lower()

        rx_list = [re.compile(p, re.I) for p in (b.get("patterns") or []) if isinstance(p, str) and p.strip()]
        if not rx_list:
            continue

        v_rx_list = [re.compile(p, re.I) for p in (b.get("value_regexes") or []) if isinstance(p, str) and p.strip()]

        out.append(
            {
                "canon": canon,
                "std": std,
                "group": b.get("group"),
                "value_type": vtype,
                "rx_list": rx_list,
                "value_rx_list": v_rx_list,
                "status_lexicon": b.get("status_lexicon") or {},
                "normalize_map": b.get("normalize_map") or {},
            }
        )

    return out
