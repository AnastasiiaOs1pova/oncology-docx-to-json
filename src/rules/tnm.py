from __future__ import annotations

import re
from typing import Dict, Optional

from .text_utils import normalize_confusables_to_latin

RE_TNM = re.compile(
    r"\b(?P<prefix>(?:c|p|yc|yp|y|r)?)\s*"
    r"T\s*(?P<t>(?:is)|(?:0|1|2|3|4)(?:[a-d])?)\s*"
    r"N\s*(?P<n>(?:0|1|2|3)(?:[a-c])?|x)\s*"
    r"M\s*(?P<m>(?:0|1(?:[a-c])?)|x)\b",
    flags=re.IGNORECASE,
)


def extract_tnm(text: str) -> Optional[Dict[str, str]]:
    if not text:
        return None
    t = normalize_confusables_to_latin(text)
    m = RE_TNM.search(t)
    if not m:
        return None
    return {
        "t": f"T{m.group('t')}".upper(),
        "n": f"N{m.group('n')}".upper(),
        "m": f"M{m.group('m')}".upper(),
    }
