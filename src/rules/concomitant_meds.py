# src/rules/concomitant_meds.py
from __future__ import annotations

import re
from pathlib import Path
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple


def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ----------------------------
# Поиск “блоков” сопутствующей терапии
# ----------------------------
RE_MEDS_HEAD = re.compile(
    r"(?im)^\s*(?:сопутствующ\w+\s+терапи\w+|соп\.\s*терапи\w+|"
    r"постоянно\s+принима\w+|принима\w+\s+постоянно|"
    r"на\s+постоянн\w+\s+основе|"
    r"meds|medications)\s*[:\-]?\s*(?P<body>.*)$"
)

RE_MEDS_SENT = re.compile(
    r"(?is)\b(постоянно\s+принима\w+|сопутствующ\w+\s+терапи\w+|на\s+фоне\s+прием\w+)\b[^.\n]{0,360}"
)

# ----------------------------
# Доза / путь / частота / даты
# ----------------------------
RE_DOSE = re.compile(r"(?i)\b(?P<val>\d+(?:[.,]\d+)?)\s*(?P<unit>мг/м2|мг|г|мкг|ед|ме|iu|мл)\b")
RE_ROUTE = re.compile(r"(?i)\b(в/в|в/м|п/к|per\s*os|p\.?\s*o\.?|внутрь)\b")
RE_FREQ = re.compile(
    r"(?i)\b("
    r"\d+\s*(?:раза?|р)\s*(?:в\s*сутки|в\s*день|/сут|/д|в\s*неделю)|"
    r"каждые\s*\d+\s*(?:ч|час(а|ов)?)|"
    r"ежедневно|через\s*день|утром|вечером|на\s+ночь"
    r")\b"
)

RE_DATE_MY = re.compile(r"\b(\d{2})\.(\d{4})\b")
RE_DATE_DMY = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
RE_DATE_Y = re.compile(r"\b(19\d{2}|20\d{2})\b")

RE_START = re.compile(r"(?i)\bс\s*(\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{4}|\d{4})\b")
RE_END = re.compile(r"(?i)\b(?:по|до)\s*(\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{4}|\d{4})\b")


def date_to_iso_like(s: str) -> str:
    s = (s or "").strip()
    m = RE_DATE_DMY.search(s)
    if m:
        dd, mm, yy = m.groups()
        return f"{yy}-{mm}-{dd}"
    m = RE_DATE_MY.search(s)
    if m:
        mm, yy = m.groups()
        return f"{yy}-{mm}"
    m = RE_DATE_Y.search(s)
    if m:
        return m.group(1)
    return s


def _repo_root() -> Path:
    # src/rules/concomitant_meds.py -> repo_root = parents[2]
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load_drug_vocab() -> List[str]:
    """
    Берём oncology/resources/drugs.txt (если есть).
    Если нет — просто возвращаем пустой список (парсинг по “сырым” строкам блока всё равно будет).
    """
    p = _repo_root() / "resources" / "drugs.txt"
    if not p.exists():
        return []
    lines = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        lines.append(_norm_spaces(ln.lower()))
    # длинные названия сначала (чтобы "интерферон альфа-2a" не резался)
    lines.sort(key=len, reverse=True)
    return lines


def _extract_drug_candidates_from_block(block: str) -> List[str]:
    """
    Очень обширно: берём элементы после заголовка и делим по ; / , / переносам.
    """
    s = (block or "").strip()
    s = s.replace("\n", "; ")
    parts = [p.strip() for p in re.split(r"[;]", s) if p.strip()]
    # если всё в одной строке через запятые
    out: List[str] = []
    for p in parts:
        if p.count(",") >= 1:
            out.extend([x.strip() for x in p.split(",") if x.strip()])
        else:
            out.append(p)
    # ограничим “мусор”
    return [x[:220] for x in out if len(x) >= 3]


def _parse_one_item(raw: str) -> Dict[str, Any]:
    """
    Собирает объект под схему:
    {drug, dose_value, dose_unit, route, frequency, start_date, end_date, source}
    """
    txt = _norm_spaces(raw)

    dose_value: Optional[float] = None
    dose_unit: Optional[str] = None
    m = RE_DOSE.search(txt)
    if m:
        dose_unit = m.group("unit").lower()
        try:
            dose_value = float(m.group("val").replace(",", "."))
        except Exception:
            dose_value = None

    route = None
    m = RE_ROUTE.search(txt)
    if m:
        route = m.group(1).lower()

    frequency = None
    m = RE_FREQ.search(txt)
    if m:
        frequency = _norm_spaces(m.group(1).lower())

    start_date = None
    m = RE_START.search(txt)
    if m:
        start_date = date_to_iso_like(m.group(1))

    end_date = None
    m = RE_END.search(txt)
    if m:
        end_date = date_to_iso_like(m.group(1))

    # drug name: всё до дозы/частоты/пути, иначе первые 1–5 слов
    drug = txt
    if m := RE_DOSE.search(drug):
        drug = drug[: m.start()]
    if m := RE_ROUTE.search(drug):
        drug = drug[: m.start()]
    if m := RE_FREQ.search(drug):
        drug = drug[: m.start()]
    drug = drug.strip(" -—:;,.")

    # если слишком длинно — возьмём первые 6 “слов”
    if len(drug) > 80:
        drug = " ".join(drug.split()[:6])

    return {
        "drug": drug or None,
        "dose_value": dose_value,
        "dose_unit": dose_unit,
        "route": route,
        "frequency": frequency,
        "start_date": start_date,
        "end_date": end_date,
        "source": None,  # поставим снаружи
    }


def extract_concomitant_meds(
    text: str,
    *,
    include_weak_mentions: bool = True,
    max_items: int = 80,
) -> List[Dict[str, Any]]:
    """
    Пытается извлечь сопутствующую терапию максимально широко, но детерминированно.

    Возвращает список под схему:
      {drug, dose_value, dose_unit, route, frequency, start_date, end_date, source}
    """
    t = text or ""
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(raw_item: str, ctx: str, confidence: str) -> None:
        obj = _parse_one_item(raw_item)
        drug = (obj.get("drug") or "")
        if not drug:
            return
        key = _norm_spaces(drug).lower()
        if key in seen:
            return
        seen.add(key)
        obj["source"] = f"confidence={confidence}; правила: сопутствующая терапия: {(_norm_spaces(ctx)[:240])}"
        out.append(obj)

    # 1) Strong: заголовки
    for m in RE_MEDS_HEAD.finditer(t):
        ctx = m.group(0)
        body = m.group("body") or ""
        items = _extract_drug_candidates_from_block(body)
        for it in items:
            add(it, ctx, "strong")
            if len(out) >= max_items:
                return out

    if not include_weak_mentions:
        return out

    # 2) Weak: предложения “постоянно принимает…”
    for m in RE_MEDS_SENT.finditer(t):
        ctx = m.group(0)
        tail = ctx
        # всё после ключевой фразы
        tail = re.sub(r"(?is).*?\b(постоянно\s+принима\w+|сопутствующ\w+\s+терапи\w+|на\s+фоне\s+прием\w+)\b", "", tail)
        tail = tail.strip(" :\-—")
        if not tail:
            continue
        items = _extract_drug_candidates_from_block(tail)
        for it in items:
            add(it, ctx, "weak")
            if len(out) >= max_items:
                return out

    return out