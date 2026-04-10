# src/rules/comorbidities.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ----------------------------
# Заголовки / секции
# ----------------------------
RE_SECTION_HEAD = re.compile(
    r"(?im)^\s*(?P<h>"
    r"(?:сопутствующ\w+\s+(?:патолог\w+|заболеван\w+)|"
    r"коморбидн\w+|"
    r"анамнез\s+жизни|"
    r"анамнез|"
    r"из\s+сопутствующ\w+|"
    r"соп\.\s*патолог\w+|"
    r"фонов\w+\s+заболеван\w+|"
    r"хрон\.\s*заболеван\w+|"
    r"status\s+praesens|"
    r"соматическ\w+\s+статус)"
    r")\s*[:\-]?\s*$"
)

RE_SECTION_STOP = re.compile(
    r"(?im)^\s*(?:диагноз|онкоанамнез|локальн\w+ статус|"
    r"объективн\w+|осмотр|лечени\w+|проведен\w+|назначен\w+|"
    r"терапи\w+|операци\w+|лучев\w+|исследован\w+|мги|игх|"
    r"лабораторн\w+|анализ\w+|рекоменд\w+)\b.*$"
)


def _iter_sections(text: str, max_chars: int = 3500) -> List[Tuple[str, str]]:
    """
    Возвращает список (heading, body) для секций сопутствующих.
    """
    t = text or ""
    out: List[Tuple[str, str]] = []

    for m in RE_SECTION_HEAD.finditer(t):
        h = _norm_spaces(m.group("h"))
        start = m.end()
        end = min(len(t), start + max_chars)
        chunk = t[start:end]

        # обрежем на следующем "стоп-заголовке"
        stop = RE_SECTION_STOP.search(chunk)
        if stop:
            chunk = chunk[: stop.start()]

        chunk = chunk.strip()
        if chunk:
            out.append((h, chunk))
    return out


# ----------------------------
# Набор “широких, но контролируемых” паттернов
# ----------------------------
@dataclass(frozen=True)
class ComorbPattern:
    name: str
    rx: re.Pattern[str]


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, flags=re.IGNORECASE)


COMORBIDITY_PATTERNS: List[ComorbPattern] = [
    # Сердечно-сосудистые
    ComorbPattern("артериальная гипертензия", _rx(r"\b(артериальн\w+\s+гипертенз\w+|гипертонич\w+\s+болезн\w+|\bАГ\b|\bГБ\b)\b")),
    ComorbPattern("ишемическая болезнь сердца", _rx(r"\b(ишемическ\w+\s+болезн\w+\s+сердца|\bИБС\b|стенокард\w+)\b")),
    ComorbPattern("хроническая сердечная недостаточность", _rx(r"\b(хроническ\w+\s+сердечн\w+\s+недостаточн\w+|\bХСН\b)\b")),
    ComorbPattern("фибрилляция предсердий", _rx(r"\b(фибрилляц\w+\s+предсерд\w+|\bФП\b|мерцательн\w+\s+аритм\w+)\b")),
    ComorbPattern("инфаркт миокарда в анамнезе", _rx(r"\b(инфаркт\w+\s+миокард\w+)\b")),
    ComorbPattern("инсульт/ОНМК в анамнезе", _rx(r"\b(инсульт\w+|\bОНМК\b|нарушен\w+\s+мозгов\w+\s+кровообращен\w+)\b")),
    ComorbPattern("тромбоз/ТЭЛА", _rx(r"\b(тромбоз\w+|\bТЭЛА\b|тромбоэмбол\w+\s+легочн\w+\s+артер\w+)\b")),

    # Эндокринные / метаболические
    ComorbPattern("сахарный диабет", _rx(r"\b(сахарн\w+\s+диабет\w+|\bСД\b|diabetes)\b")),
    ComorbPattern("сахарный диабет 2 типа", _rx(r"\b(сд\s*2|сахарн\w+\s+диабет\w+\s*2)\b")),
    ComorbPattern("сахарный диабет 1 типа", _rx(r"\b(сд\s*1|сахарн\w+\s+диабет\w+\s*1)\b")),
    ComorbPattern("ожирение", _rx(r"\b(ожирен\w+|\bИМТ\b\s*>\s*\d+)\b")),
    ComorbPattern("дислипидемия/гиперхолестеринемия", _rx(r"\b(дислипидем\w+|гиперхолестеринем\w+)\b")),
    ComorbPattern("подагра", _rx(r"\bподагр\w+\b")),
    ComorbPattern("гипотиреоз", _rx(r"\bгипотиреоз\w*\b")),
    ComorbPattern("гипертиреоз/тиреотоксикоз", _rx(r"\b(тиреотоксикоз\w*|гипертиреоз\w*)\b")),

    # Почки / печень
    ComorbPattern("хроническая болезнь почек", _rx(r"\b(хроническ\w+\s+болезн\w+\s+почек|\bХБП\b|\bХПН\b|почечн\w+\s+недостаточн\w+)\b")),
    ComorbPattern("мочекаменная болезнь", _rx(r"\b(мочекаменн\w+\s+болезн\w+|\bМКБ\b)\b")),
    ComorbPattern("хронический гепатит", _rx(r"\b(хроническ\w+\s+гепатит\w+|гепатит\s*[bc])\b")),
    ComorbPattern("цирроз печени", _rx(r"\bцирроз\w+\s+печен\w+\b")),
    ComorbPattern("жировая болезнь печени (стеатоз)", _rx(r"\b(стеатоз\w+|жиров\w+\s+гепатоз\w+)\b")),

    # Дыхательная система
    ComorbPattern("бронхиальная астма", _rx(r"\b(бронхиальн\w+\s+астм\w+|\bБА\b)\b")),
    ComorbPattern("ХОБЛ", _rx(r"\b(хобл|хроническ\w+\s+обструктивн\w+\s+болезн\w+\s+легк\w+)\b")),
    ComorbPattern("туберкулез (в т.ч. в анамнезе)", _rx(r"\bтуберкулез\w*\b")),

    # ЖКТ
    ComorbPattern("язвенная болезнь", _rx(r"\b(язвенн\w+\s+болезн\w+|язва\s+желудк\w+|язва\s+двенадцатиперстн\w+)\b")),
    ComorbPattern("гастрит/ГЭРБ", _rx(r"\b(гастрит\w+|гэрб|гастроэзофагеальн\w+\s+рефлюкс\w+)\b")),
    ComorbPattern("панкреатит", _rx(r"\bпанкреатит\w*\b")),
    ComorbPattern("воспалительные заболевания кишечника", _rx(r"\b(болезн\w+\s+крон\w+|язвенн\w+\s+колит\w+)\b")),

    # Неврология / психиатрия
    ComorbPattern("эпилепсия", _rx(r"\bэпилепси\w+\b")),
    ComorbPattern("депрессия/тревожное расстройство", _rx(r"\b(депресс\w+|тревожн\w+\s+расстройств\w+)\b")),

    # Инфекции / иммунный статус
    ComorbPattern("ВИЧ-инфекция", _rx(r"\b(вич|HIV)\b")),
    ComorbPattern("хроническая инфекция (HBV/HCV)", _rx(r"\b(HBV|HCV)\b")),

    # Кровь / коагуляция
    ComorbPattern("анемия (хроническая/сопутствующая)", _rx(r"\bанеми\w+\b")),
    ComorbPattern("тромбоцитопения", _rx(r"\bтромбоцитопени\w+\b")),
    ComorbPattern("нарушение свертывания/коагулопатия", _rx(r"\b(коагулопат\w+|нарушен\w+\s+свертыван\w+)\b")),
]


# ----------------------------
# Эвристики статуса/тяжести
# ----------------------------
RE_RESOLVED_HINT = re.compile(r"\b(в\s+анамнезе|перенес\w+|ранее|в\s+прошлом|после)\b", flags=re.IGNORECASE)
RE_ACTIVE_HINT = re.compile(r"\b(страда\w+|наблюда\w+|получа\w+\s+терапи\w+|принима\w+|на\s+фоне)\b", flags=re.IGNORECASE)

RE_SEVERITY = re.compile(
    # ВАЖНО: в ИБ часто встречается "корня легкого" и т.п.
    # Нельзя трактовать "легкого" (орган) как "легкая" (тяжесть).
    # Поэтому тяжесть извлекаем только при явной конструкции "... степени/течения/выраженности".
    r"\b(тяжел\w+|среднетяжел\w+|умерен\w+|легк\w+)\s+(?:степен\w+|течен\w+|форма|выраженност\w+)\b",
    flags=re.IGNORECASE,
)
RE_STAGE_GRADE = re.compile(
    r"\b(стад\w+\s*\d+|ст\.\s*\d+|степен\w+\s*\d+|класс\s*[IVX]+|NYHA\s*[IVX]+)\b",
    flags=re.IGNORECASE
)


def _infer_status(ctx: str) -> str:
    c = ctx or ""
    if RE_RESOLVED_HINT.search(c) and not RE_ACTIVE_HINT.search(c):
        return "resolved"
    if RE_ACTIVE_HINT.search(c):
        return "active"
    return "unknown"


def _infer_severity(ctx: str) -> Optional[str]:
    c = ctx or ""
    m = RE_SEVERITY.search(c)
    if m:
        return m.group(1).lower()
    m2 = RE_STAGE_GRADE.search(c)
    if m2:
        return _norm_spaces(m2.group(1))
    return None


# ----------------------------
# Спец-случаи без домыслов (буквально по тексту)
# ----------------------------

RE_DIVERT_DUOD = re.compile(
    r"(?i)\bдивертикул(?:ы)?\s+(?:12\s*\-?\s*перстн\w+|двенадцатиперстн\w+)\s+кишк\w+\b"
)
RE_DIVERT_SIGM = re.compile(r"(?i)\bдивертикул(?:ы)?\s+сигмовидн\w+\s+кишк\w+\b")


# ----------------------------
# Основная функция
# ----------------------------
def extract_comorbidities(
    text: str,
    *,
    include_weak_mentions: bool = True,
    max_items: int = 60,
) -> List[Dict[str, Any]]:
    """
    Возвращает список объектов под схему:
    {name, severity, status, source}

    include_weak_mentions:
      - True: ищем и в секциях, и по всему тексту (weak)
      - False: только секции (strong)
    """
    t = text or ""

    found: List[Dict[str, Any]] = []
    seen = set()

    _MISSING = object()

    def add(
        name: str,
        ctx: str,
        confidence: str,
        *,
        severity_override: object = _MISSING,
        status_override: object = _MISSING,
    ) -> None:
        nm = _norm_spaces(name)
        if not nm:
            return
        key = nm.lower()
        if key in seen:
            return
        seen.add(key)
        item = {
            "name": nm,
            "severity": _infer_severity(ctx) if severity_override is _MISSING else severity_override,
            "status": _infer_status(ctx) if status_override is _MISSING else status_override,
            "source": f"confidence={confidence}; правила: сопутствующее: {(_norm_spaces(ctx)[:240])}",
        }
        found.append(item)

    # 0) Спец-случаи: дивертикулы кишечника (без домыслов)
    # Требование: только буквальное значение из текста, без "дивертикулит" и без "легких".
    for rx in (RE_DIVERT_DUOD, RE_DIVERT_SIGM):
        for m in rx.finditer(t):
            a, b = m.span()
            ctx = t[max(0, a - 120): min(len(t), b + 160)]
            # severity строго не выводим (null), даже если рядом есть "легкого" (орган)
            add(m.group(0), ctx, "weak", severity_override=None, status_override="unknown")
            if len(found) >= max_items:
                return found

    # 1) Strong: секции
    for head, body in _iter_sections(t):
        ctx = f"{head}: {body}"
        for pat in COMORBIDITY_PATTERNS:
            if pat.rx.search(body) or pat.rx.search(head):
                add(pat.name, ctx, "strong")
                if len(found) >= max_items:
                    return found

    if not include_weak_mentions:
        return found

    # 2) Weak: по всему тексту (контролируемо)
    # Берём окно вокруг совпадения, чтобы статус/тяжесть извлекались из контекста.
    for pat in COMORBIDITY_PATTERNS:
        for m in pat.rx.finditer(t):
            a, b = m.span()
            ctx = t[max(0, a - 160): min(len(t), b + 180)]
            add(pat.name, ctx, "weak")
            if len(found) >= max_items:
                return found

    return found