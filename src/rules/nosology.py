from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

from .paths import NOSOLOGY_ALIASES_PATH


@lru_cache(maxsize=1)
def load_nosology_bundle() -> Dict[str, Any]:
    if not NOSOLOGY_ALIASES_PATH.exists():
        raise FileNotFoundError(f"Не найден nosology_aliases.json: {NOSOLOGY_ALIASES_PATH}")
    return json.loads(NOSOLOGY_ALIASES_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def get_nosology_rules_compiled():
    data = load_nosology_bundle()
    neg = data.get("negation", {}) or {}
    win = int(neg.get("window_chars", 60))
    neg_triggers = [re.compile(p, re.I) for p in (neg.get("triggers") or [])]

    rules = []
    for r in data.get("rules", []) or []:
        canonical = r.get("canonical")
        if not canonical:
            continue
        profile = r.get("profile", "unknown")
        pr = int(r.get("priority", 0))
        pats = [re.compile(p, re.I) for p in (r.get("patterns") or [])]
        if not pats:
            continue
        rules.append((pr, canonical, profile, pats))

    rules.sort(reverse=True, key=lambda x: x[0])
    return rules, win, neg_triggers


def extract_nosology(text: str) -> Tuple[Optional[str], str]:
    rules, win, neg_triggers = get_nosology_rules_compiled()
    t = text or ""

    for pr, canonical, profile, pats in rules:
        for rx in pats:
            m = rx.search(t)
            if not m:
                continue

            # negation window before match
            left = t[max(0, m.start() - win) : m.start()]
            if any(nrx.search(left) for nrx in neg_triggers):
                continue

            return canonical, (profile or "unknown")

    return None, "unknown"
