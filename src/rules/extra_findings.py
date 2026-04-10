from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .dates import DATE_ANY, date_to_iso_like
from .text_utils import norm_spaces


# -------------------------
# Metastases (very conservative)
# -------------------------
# Примеры: "мтс в печень", "mts в л/у", "метастазы в кости"
_RE_MTS = re.compile(
    # Консервативно: берём только явные конструкции "мтс/метастазы ... в/по <куда>".
    # Длина site ограничена, чтобы не захватывать дозы/фракции целиком.
    r"(?i)(?:\bмтс\b|\bmts\b|метастаз\w*)\s*(?:в|по)\s*(?P<site>[А-Яа-яA-Za-z0-9/\-\s]{2,80})"
)

_RE_DATE_NEAR = re.compile(rf"(?P<d>{DATE_ANY})", flags=re.IGNORECASE)

_RE_PROGRESS_FROM = re.compile(rf"(?i)прогрессирован\w*[^\n\.]{0,80}?\b(?:от|с)\s*(?P<d>{DATE_ANY})")


def _sent_window(text: str, start: int, end: int, *, limit: int = 360) -> str:
    """Достаём фрагмент примерно одной фразой вокруг совпадения (чтобы не путать даты из других событий)."""
    t = text or ""
    a = max(0, start - limit)
    b = min(len(t), end + limit)
    frag = t[a:b]
    # границы "предложений" внутри frag.
    # ВАЖНО: точка внутри даты (08.2021) не является разделителем.
    rel_s = start - a
    rel_e = end - a

    def is_date_dot(i: int) -> bool:
        return (
            0 < i < len(frag) - 1
            and frag[i] == "."
            and frag[i - 1].isdigit()
            and frag[i + 1].isdigit()
        )

    # ищем слева ближайший \n или "." (не внутри даты)
    left = -1
    for i in range(rel_s - 1, -1, -1):
        ch = frag[i]
        if ch == "\n":
            left = i
            break
        if ch == "." and not is_date_dot(i):
            left = i
            break

    # ищем справа ближайший \n или "." (не внутри даты)
    right = -1
    for i in range(rel_e, len(frag)):
        ch = frag[i]
        if ch == "\n":
            right = i
            break
        if ch == "." and not is_date_dot(i):
            right = i
            break

    s = frag[(left + 1) if left != -1 else 0 : right if right != -1 else len(frag)]
    return norm_spaces(s)


def _clean_site(site: str) -> Optional[str]:
    s = norm_spaces(site)
    # отсекаем хвосты по пунктуации
    s = re.split(r"[\n\r\.;:,\)\(]", s)[0].strip()

    # не допускаем попадания доз/фракций в site
    s = re.sub(r"(?i)\bсо\s+средн\w+\s+доз\w+.*$", "", s)
    s = re.sub(r"(?i)\bсод\b.*$", "", s)
    s = re.sub(r"(?i)\b\d+(?:[\.,]\d+)?\s*(?:грей|гр|gy)\b.*$", "", s)
    s = re.sub(r"(?i)[x×]\s*\d+.*$", "", s)
    # обрывки "от 03" / "от 03.2023" в конце
    s = re.sub(r"(?i)\s+от\s+\d{1,2}(?:\.\d{4}|\.\d{1,2}\.\d{2,4})?\s*$", "", s)

    s = norm_spaces(s.strip(" \t-–—"))
    if len(s) < 3:
        return None
    return s


def _pick_date_for_metastasis(window: str) -> Optional[str]:
    """Очень строгий выбор даты: сначала ищем "прогрессирование от ..." в том же фрагменте."""
    w = window or ""
    pm = _RE_PROGRESS_FROM.search(w)
    if pm:
        return date_to_iso_like(pm.group("d"))

    # затем ищем ближайшую дату, но только внутри этого фрагмента
    dm = _RE_DATE_NEAR.search(w)
    return date_to_iso_like(dm.group("d")) if dm else None


def extract_metastases(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []
    seen = set()

    for m in _RE_MTS.finditer(t):
        raw_site = m.group("site") or ""
        site = _clean_site(raw_site) or ""
        if not site:
            continue

        window = _sent_window(t, m.start(), m.end(), limit=420)
        dt: Optional[str] = _pick_date_for_metastasis(window)

        key = (site.lower(), dt or "")
        if key in seen:
            continue
        seen.add(key)

        out.append(
            {
                "site": site,
                "date": dt,
                "source": norm_spaces(window)[:260],
            }
        )

    return out


# -------------------------
# Procedures (surgery etc.) and radiotherapy (performed only)
# -------------------------

_RE_PROC = re.compile(
    r"(?i)\b(?:выполнен\w*|проведен\w*|проведено|сделан\w*|перенес\w*|перенесла|перенесено)\b[^\n]{0,140}\b"
    r"(?:операц\w*|мастэктом\w*|резекц\w*|лобэктом\w*|пульмонэктом\w*|гастрэктом\w*|колэктом\w*|нефрэктом\w*|"
    r"лимфаденэктом\w*|биопс\w*|удален\w*|иссечен\w*)\b"
)

# Номинативные формулировки без глагола (например, "Удаление импланта ... 18.09.2023").
# Очень строго: только "удаление" + устройство + дата, и НЕ должно быть слов плана/рекомендаций.
_RE_PROC_DEVICE = re.compile(
    rf"(?i)\bудаление\s+(?P<what>имплант\w*|порт\-?систем\w*)[^\n]{{0,80}}?(?P<d>{DATE_ANY})"
)

_RE_PLAN_WORDS = re.compile(r"(?i)\b(планир\w+|рекоменд\w+|возможн\w+|рассмотр\w+|показан\w+|целесообразн\w+)\b")

_RE_RT = re.compile(
    r"(?i)\b(?:проведен\w*|получил\w*|выполнен\w*|проводил\w*|проводилась|проводилось)\b[^\n]{0,160}\b"
    r"(?:лучев\w*\s*терап\w*|радиотерап\w*|облучен\w*|\bлт\b|стереотакс\w*\s*лт|кибер\s*нож|гамма\s*нож)\b"
)


def extract_procedures(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []
    seen = set()

    for m in _RE_PROC.finditer(t):
        lo = max(0, m.start() - 40)
        hi = min(len(t), m.end() + 40)
        window = t[lo:hi]
        ev = norm_spaces(t[m.start():m.end()])[:220]

        dm = _RE_DATE_NEAR.search(window)
        dt: Optional[str] = date_to_iso_like(dm.group("d")) if dm else None

        key = (ev.lower(), dt or "")
        if key in seen:
            continue
        seen.add(key)

        out.append({"date": dt, "name": ev, "source": norm_spaces(window)[:260]})

    # добавляем "удаление импланта/порт-системы" даже без глагола (если есть дата)
    for m in _RE_PROC_DEVICE.finditer(t):
        lo = max(0, m.start() - 60)
        hi = min(len(t), m.end() + 60)
        window = t[lo:hi]
        if _RE_PLAN_WORDS.search(window):
            continue
        dt = date_to_iso_like(m.group("d"))
        what = norm_spaces(m.group("what") or "")
        # name — строго буквальный фрагмент
        ev = norm_spaces(t[m.start():m.end()])[:220]
        key = (ev.lower(), dt or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": dt, "name": ev, "source": norm_spaces(window)[:260]})

    return out


def extract_radiotherapy(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []
    seen = set()

    for m in _RE_RT.finditer(t):
        lo = max(0, m.start() - 60)
        hi = min(len(t), m.end() + 60)
        window = t[lo:hi]
        ev = norm_spaces(t[m.start():m.end()])[:240]

        # даты: если есть диапазон "с ... по ..." — проставляем start/end.
        start_dt: Optional[str] = None
        end_dt: Optional[str] = None
        rm = re.search(rf"(?i)\bс\s*(?P<d1>{DATE_ANY})\s*(?:по|[\-–—])\s*(?P<d2>{DATE_ANY})", window)
        if rm:
            start_dt = date_to_iso_like(rm.group("d1"))
            end_dt = date_to_iso_like(rm.group("d2"))
        else:
            dm = _RE_DATE_NEAR.search(window)
            start_dt = date_to_iso_like(dm.group("d")) if dm else None
            em = re.search(rf"(?i)\bпо\s*(?P<d2>{DATE_ANY})", window)
            if em and start_dt:
                end_dt = date_to_iso_like(em.group("d2"))

        # схема radiotherapy: site/start_date/end_date/technique/source
        # Чтобы не додумывать: site/technique не заполняем, только даты если есть.
        key = (ev.lower(), start_dt or "", end_dt or "")
        if key in seen:
            continue
        seen.add(key)

        out.append(
            {
                "site": None,
                "start_date": start_dt,
                "end_date": end_dt,
                "technique": None,
                "source": norm_spaces(window)[:260],
            }
        )

    return out
