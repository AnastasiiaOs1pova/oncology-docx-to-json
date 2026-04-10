from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .biomarkers_catalog import load_biomarkers_catalog_compiled
from .dates import DATE_DMY, DATE_Y, date_to_iso_like
from .text_utils import norm_spaces, num_normalize


# =============================
# Biomarker blocks (safe zones)
# =============================

_NEXT_IHC = rf"(?:\b(?:Гистология\s*и\s*ИГХ|ГИ\s*и\s*ИГХ|ИГХ)\b[^\n]{{0,180}}?\bот\s*{DATE_DMY})"
_NEXT_MGI = rf"(?:\bМГИ\b[^\n]{{0,260}}?\bот\s*{DATE_DMY})"

RE_IHC_BLOCK = re.compile(
    rf"(?P<head>(?:Гистология\s*и\s*ИГХ|ГИ\s*и\s*ИГХ|ИГХ)[^\n]{{0,180}}?)"
    rf"(?:\s*№\s*\S+)?\s*"
    rf"(?:от\s*(?P<d>{DATE_DMY})\s*(?:г\.? )?|в\s+(?P<y>{DATE_Y})\s*г\.?)\s*[:\-]?\s*"
    # Для реальных ИБ блок может быть длинным (несколько тысяч символов),
    # поэтому верхнюю границу делаем более щедрой.
    rf"(?P<body>.{{0,12000}}?)"
    # ВАЖНО: останавливаемся не только на МГИ, но и на СЛЕДУЮЩЕМ ИГХ-блоке,
    # иначе дата «протекает» на последующие ИГХ-записи (напр. PD-L1 CPS 10 внутри блока 31.03.2023).
    rf"(?=(?:\n{{2,}})|{_NEXT_MGI}|{_NEXT_IHC}|\Z)",
    flags=re.IGNORECASE | re.DOTALL,
)

RE_MGI_BLOCK = re.compile(
    rf"\bМГИ\b[^\n]{{0,260}}?\bот\s*(?P<d>{DATE_DMY})\s*(?:г\.?)?\s*[:\-]?\s*"
    rf"(?P<body>.{{0,12000}}?)"
    # Аналогично: ограничиваем блок, чтобы дата МГИ не «протекала» через соседние МГИ/ИГХ.
    rf"(?=(?:\n{{2,}})|{_NEXT_IHC}|{_NEXT_MGI}|\Z)",
    flags=re.IGNORECASE | re.DOTALL,
)


# =============================
# Biomarkers: value extraction
# =============================

_RE_VALUE_NEAR = re.compile(
    r"(?P<val>" r"(?:0|1\+|2\+|3\+)" r"|(?:\d{1,3}\s*%|\d+(?:[.,]\d+)?)" r"|(?:CPS\s*=?\s*\d{1,3})" r"|(?:TPS\s*=?\s*\d{1,3}\s*%)" r")",
    flags=re.IGNORECASE,
)


def _extract_value_near(block_text: str, start: int, end: int) -> Optional[str]:
    # right side
    right = block_text[end : min(len(block_text), end + 120)]
    m = _RE_VALUE_NEAR.search(right)
    if m:
        return norm_spaces(m.group("val"))

    # left side (rare)
    left = block_text[max(0, start - 60) : start]
    m2 = _RE_VALUE_NEAR.search(left)
    if m2:
        return norm_spaces(m2.group("val"))

    return None


def _sentence_window(text: str, start: int, end: int, *, max_len: int = 420) -> str:
    """Возвращает 'предложение' вокруг совпадения.

    Важная деталь: точка внутри числа (6.42) или даты (01.02.2024) НЕ должна считаться границей.
    """
    if not text:
        return ""

    def _is_inner_numeric_dot(pos: int) -> bool:
        if pos <= 0 or pos >= len(text) - 1:
            return False
        if text[pos] != ".":
            return False
        return text[pos - 1].isdigit() and text[pos + 1].isdigit()

    def _rfind_break(ch: str, upto: int) -> int:
        p = text.rfind(ch, 0, upto)
        while p != -1 and ch == "." and _is_inner_numeric_dot(p):
            p = text.rfind(ch, 0, p)
        return p

    def _find_break(ch: str, from_pos: int) -> int:
        p = text.find(ch, from_pos)
        while p != -1 and ch == "." and _is_inner_numeric_dot(p):
            p = text.find(ch, p + 1)
        return p

    # границы слева
    left_candidates = [
        _rfind_break("\n", start),
        _rfind_break(".", start),
        _rfind_break("!", start),
        _rfind_break("?", start),
        _rfind_break(";", start),
        _rfind_break(":", start),
    ]
    left = max(left_candidates)
    left = left + 1 if left != -1 else 0

    # границы справа
    right_candidates = []
    for ch in ("\n", ".", "!", "?", ";"):
        p = _find_break(ch, end)
        if p != -1:
            right_candidates.append(p)
    right = min(right_candidates) if right_candidates else len(text)

    window = text[left:right].strip()

    # ограничение длины окна
    if len(window) > max_len:
        mid = (start + end) // 2
        a = max(0, mid - max_len // 2)
        b = min(len(text), mid + max_len // 2)
        window = text[a:b].strip()
    return window


def _status_from_lexicon_literal(snippet: str, lex: Dict[str, Any]) -> Optional[str]:
    """Возвращает БУКВАЛЬНЫЙ фрагмент, совпавший со словарём статусов.

    Важно для QC/аудита: не подменяем значение на 'positive/negative', если этих слов нет в тексте.
    """
    s = snippet or ""
    order = (lex.get("_priority") or ["negative", "positive", "unknown"])
    for key in order:
        pats = lex.get(key) or []
        for p in pats:
            m = re.search(p, s, flags=re.I)
            if m:
                # предпочитаем фактически совпавший кусок текста
                lit = (m.group(0) or "").strip()
                if lit:
                    return lit
                # fallback — если паттерн без текста (редко)
                return key
    return None


def _extract_value_for_item(text: str, m_start: int, m_end: int, item: Dict[str, Any]) -> Optional[str]:
    """1) value_regexes -> 2) status via lexicon -> 3) none -> 4) numeric fallback"""
    window = _sentence_window(text, m_start, m_end)
    if not window:
        left = max(0, m_start - 120)
        right = min(len(text), m_end + 200)
        window = text[left:right]

    vtype = (item.get("value_type") or "none").lower()

    # 1) explicit value regexes
    for vrx in item.get("value_rx_list") or []:
        mm = vrx.search(window)
        if mm:
            val = mm.group(1) if mm.lastindex else mm.group(0)
            return (val or "").strip()

    # 2) status only via lexicon (НО возвращаем буквальный матч, а не нормализованный ключ)
    if vtype == "status":
        val = _status_from_lexicon_literal(window, item.get("status_lexicon") or {})
        return (val or "").strip() if val else None

    # 3) none means “mentioned”
    if vtype == "none":
        return None

    # 4) numeric-ish fallback
    val = _extract_value_near(text, m_start, m_end)
    return (val or "").strip() if val else None


@dataclass
class Biomarker:
    name_raw: str
    name_std: str
    value: Optional[str]
    date: Optional[str]
    source: str


def _prefer_more_precise_numeric(items: List[Biomarker], *, key_name_std: str) -> List[Biomarker]:
    by_date: Dict[str, List[Biomarker]] = {}
    keep: List[Biomarker] = []

    for b in items:
        if b.name_std != key_name_std or not b.date or not b.value:
            keep.append(b)
            continue
        by_date.setdefault(b.date, []).append(b)

    for d, lst in by_date.items():
        if len(lst) == 1:
            keep.append(lst[0])
            continue
        best = max(lst, key=lambda x: len(str(x.value)))
        keep.append(best)

    final: List[Biomarker] = []
    seen = set()
    for b in keep:
        key = (b.name_std, (b.value or "").lower(), b.date or "")
        if key in seen:
            continue
        seen.add(key)
        final.append(b)
    return final


def extract_biomarkers(text: str) -> List[Biomarker]:
    """Извлекаем биомаркеры из ИГХ/МГИ safe-zones + мягкий fallback для status/variant."""
    t = text or ""
    out: List[Biomarker] = []
    catalog = load_biomarkers_catalog_compiled()

    def add(*, canon: str, std: str, value: Optional[str], date: Optional[str], prefix: str, evidence: str) -> None:
        if value is not None:
            value = num_normalize(value)

        out.append(
            Biomarker(
                name_raw=canon,
                name_std=std,
                value=value,
                date=date,
                source=f"{prefix}: "
                + (
                    f"status={value}; "
                    if (value in {"positive", "negative", "unknown"})
                    else f"value={value}; "
                    if value is not None
                    else ""
                )
                + f"{norm_spaces(evidence)[:240]}",
            )
        )

    def scan_block(block_text: str, *, date: Optional[str], prefix: str) -> None:
        if not block_text:
            return
        for item in catalog:
            canon = item["canon"]
            std = item["std"]
            for rx in item["rx_list"]:
                for m in rx.finditer(block_text):
                    val = _extract_value_for_item(block_text, m.start(), m.end(), item)
                    evidence = block_text[max(0, m.start() - 80) : min(len(block_text), m.end() + 140)]
                    # защитимся от “залипания” МГИ/других блоков внутрь ИГХ-safe-zone
                    if prefix == "ИГХ" and ("мги" in evidence.lower() or "молекул" in evidence.lower()):
                        continue
                    add(canon=canon, std=std, value=val, date=date, prefix=prefix, evidence=evidence)

    # 1) IHC safe-zone
    for m in RE_IHC_BLOCK.finditer(t):
        date: Optional[str] = None
        if m.group("d"):
            date = date_to_iso_like(m.group("d"))
        elif m.group("y"):
            date = m.group("y")
        scan_block(m.group("body") or "", date=date, prefix="ИГХ")

    # 2) MGI safe-zone
    for m in RE_MGI_BLOCK.finditer(t):
        date = date_to_iso_like(m.group("d"))
        scan_block(m.group("body") or "", date=date, prefix="МГИ")

    # 3) мягкий fallback по всему тексту (только status/variant и только если нашли значение)
    for item in catalog:
        vtype = (item.get("value_type") or "none").lower()
        if vtype not in {"status", "variant"}:
            continue
        for rx in item["rx_list"]:
            for m2 in rx.finditer(t):
                val = _extract_value_for_item(t, m2.start(), m2.end(), item)
                if not val:
                    continue
                evidence = t[max(0, m2.start() - 90) : min(len(t), m2.end() + 160)]
                add(canon=item["canon"], std=item["std"], value=val, date=None, prefix="TEXT", evidence=evidence)

    # 4) дедуп по (name_std, value, date)
    uniq: List[Biomarker] = []
    seen = set()
    for b in out:
        key = (b.name_std, (b.value or "").strip().lower(), b.date or "")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)

    # 5) подавление “без даты”, если есть такой же маркер с датой
    dated_keys = {(b.name_std, (b.value or "").strip().lower()) for b in uniq if b.date}
    uniq = [b for b in uniq if b.date or (b.name_std, (b.value or "").strip().lower()) not in dated_keys]

    # 6) эвристика точности TMB
    uniq = _prefer_more_precise_numeric(uniq, key_name_std="tmb")

    return uniq
