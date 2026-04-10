from __future__ import annotations

import re
from typing import Optional, Tuple

# accept:
# - 7.5.2017 / 07.05.2017 / 07.05.17
# - 8.2021 / 08.2021
# - 12.24 / 12.24г (месяц.год_2цифры)  -> 2024-12
# - апрель 2025 / апреля 2025г         -> 2025-04
# - 2021
RE_DATE_DMY = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s*(?:г\.?)?\s*$")
RE_DATE_MY = re.compile(r"^\s*(\d{1,2})\.(\d{4})\s*(?:г\.?)?\s*$")
RE_DATE_MY2 = re.compile(r"^\s*(\d{1,2})\.(\d{2})\s*(?:г\.?)?\s*$")
RE_DATE_Y = re.compile(r"^\s*((?:19|20)\d{2})\s*(?:г\.?)?\s*$")

# русские названия месяцев (нормализуем к YYYY-MM)
_MONTHS = {
    "январ": "01",
    "феврал": "02",
    "март": "03",
    "апрел": "04",
    "ма": "05",  # май/мая
    "июн": "06",
    "июл": "07",
    "август": "08",
    "сентябр": "09",
    "октябр": "10",
    "ноябр": "11",
    "декабр": "12",
}

RE_DATE_MONTH_Y = re.compile(
    r"^\s*(?P<mon>январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\s*(?P<yy>(?:19|20)\d{2})\s*(?:г\.?)?\s*$",
    flags=re.IGNORECASE,
)


def date_to_iso_like(s: str) -> str:
    s = (s or "").strip()

    m = RE_DATE_DMY.match(s)
    if m:
        dd, mm, yy = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        if len(yy) == 2:
            # 17 -> 2017 (до 30 считаем 20xx)
            yy = ("20" if int(yy) <= 30 else "19") + yy
        return f"{yy}-{mm}-{dd}"

    m = RE_DATE_MY.match(s)
    if m:
        mm, yy = m.group(1).zfill(2), m.group(2)
        return f"{yy}-{mm}"

    # 12.24 -> 2024-12
    m = RE_DATE_MY2.match(s)
    if m:
        mm, yy2 = m.group(1).zfill(2), m.group(2)
        yy = ("20" if int(yy2) <= 30 else "19") + yy2
        return f"{yy}-{mm}"

    m = RE_DATE_Y.match(s)
    if m:
        return m.group(1)

    # "апреля 2025" -> 2025-04
    m = RE_DATE_MONTH_Y.match(s)
    if m:
        mon = (m.group("mon") or "").lower()
        yy = m.group("yy")
        mm = None
        for k, v in _MONTHS.items():
            if mon.startswith(k):
                mm = v
                break
        if mm:
            return f"{yy}-{mm}"

    return s


DATE_DMY = r"(?:\d{1,2}\.\d{1,2}\.\d{2,4})"
DATE_MY = r"(?:\d{1,2}\.\d{4})"
DATE_MY2 = r"(?:\d{1,2}\.\d{2})"  # 12.24
DATE_Y = r"(?:19\d{2}|20\d{2})"
DATE_MONTH_Y = r"(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\s*(?:19\d{2}|20\d{2})"
DATE_ANY = rf"(?:{DATE_DMY}|{DATE_MY}|{DATE_MY2}|{DATE_MONTH_Y}|{DATE_Y})"

RE_RANGE = re.compile(
    rf"(?:с|c)\s*(?P<start>{DATE_ANY})\s*(?:г\.?)?\s*" rf"(?:по|-|—)\s*" rf"(?P<end>{DATE_ANY})\s*(?:г\.?)?",
    flags=re.IGNORECASE,
)


def parse_range(range_str: str) -> Tuple[Optional[str], Optional[str]]:
    m = RE_RANGE.search(range_str or "")
    if not m:
        return (None, None)
    return (date_to_iso_like(m.group("start")), date_to_iso_like(m.group("end")))


def sort_key_date(s: Optional[str]) -> Tuple[int, int, int]:
    if not s:
        return (0, 0, 0)
    s = str(s).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return (int(m.group(1)), 0, 0)
    return (0, 0, 0)
