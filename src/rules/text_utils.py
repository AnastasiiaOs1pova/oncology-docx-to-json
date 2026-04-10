from __future__ import annotations

import re


def norm_spaces(s: str) -> str:
    return " ".join((s or "").replace("\r", "\n").replace("\t", " ").split())


def num_normalize(val: str) -> str:
    return (val or "").replace(",", ".").strip()


# =============================
# Confusable Cyrillic/Latin letters (TNM/prefixes)
# =============================

# В клинических текстах часто встречается "псевдолатиница" (кириллица, визуально похожая на латиницу):
#   рT1bN0M0, сT2N0M0 и т.п. Это ломает регулярки.
# Нормализуем такие буквы в латиницу ДО поиска.
_CONFUSABLE_LATIN_MAP = str.maketrans(
    {
        # prefixes
        "р": "p",
        "Р": "p",  # кириллическая "эр" визуально как латинская p
        "у": "y",
        "У": "y",  # для 'yc/yp' встречается как 'ус/уп'

        # stage letters
        "с": "c",
        "С": "c",
        "т": "t",
        "Т": "T",
        "н": "n",
        "Н": "N",
        "м": "m",
        "М": "M",
        "х": "x",
        "Х": "X",
    }
)


def normalize_confusables_to_latin(s: str) -> str:
    return (s or "").translate(_CONFUSABLE_LATIN_MAP)


def strip_trailing_punct(s: str) -> str:
    return (s or "").strip(" -—:;,")


def split_before_date_words(s: str) -> str:
    """Обрезаем хвост после слов "с/по/от" перед датой, чтобы вынуть режим терапии."""
    if not s:
        return ""
    return re.split(r"(?:\bс\b|\bc\b|\bпо\b|\bот\b)\s*\d", s, 1, flags=re.IGNORECASE)[0]
