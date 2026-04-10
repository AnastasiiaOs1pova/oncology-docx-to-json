# src/rules/allergies.py
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .concomitant_meds import load_drug_vocab


def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


RE_ALLERGY_HEAD = re.compile(
    r"(?im)^\s*(аллерг\w+|аллергоанамнез|непереносимост\w+)\s*[:\-]?\s*(?P<body>.*)$"
)

RE_ALLERGY_SENT = re.compile(
    r"(?is)\b(аллерг\w+|непереносимост\w+|гиперчувствительн\w+)\b[^.\n]{0,260}"
)

RE_ON = re.compile(r"(?i)\b(на|к)\b\s+(?P<sub>[^,;\n\.]{2,80})")
RE_REACTION = re.compile(
    r"(?i)\b(анафилакси\w+|сып\w+|крапивниц\w+|отек\s+квинке|бронхоспазм\w+|зуд\w+|"
    r"тошнот\w+|рвот\w+|диаре\w+|лихорадк\w+)\b"
)

RE_EXPLICIT_SEVERITY = re.compile(
    r"(?i)\b(л[её]гк\w+|умерен\w+|тяж[её]л\w+)\b(?:\s+степен\w+|\s+течен\w+|\s+реакци\w+)?"
)


def _infer_severity(ctx: str) -> Optional[str]:
    """Только явная тяжесть из текста.

    Никаких выводов по симптомам (анафилаксия/сыпь/зуд) — это домысел для строгого аудита.
    """
    m = RE_EXPLICIT_SEVERITY.search(ctx or "")
    if not m:
        return None
    w = (m.group(1) or "").lower()
    if w.startswith("л"):
        return "mild"
    if w.startswith("у"):
        return "moderate"
    if w.startswith("т"):
        return "severe"
    return None


def _clean_substance(s: str) -> str:
    x = _norm_spaces(s)
    # убрать служебные префиксы
    x = re.sub(r"(?i)^\s*(аллерг\w+|аллергоанамнез|непереносимост\w+|гиперчувствительн\w+)\s*[:\-—]*\s*", "", x)
    x = re.sub(r"(?i)^\s*(реакци\w+|аллергическ\w+\s+реакци\w+)\s*[:\-—]*\s*", "", x)
    x = re.sub(r"\bпрепарат\w*\b", "", x, flags=re.I)
    # обрезаем хвосты типа ")+Цисплатин" / "+..."
    x = re.split(r"[\)\+;\n]", x, maxsplit=1)[0]
    x = x.strip(" -—:;,.()[]")
    # обрежем слишком длинное
    return x[:80].strip()


def _clean_reaction(ctx: str) -> Optional[str]:
    m = RE_REACTION.search(ctx or "")
    if m:
        return m.group(1).lower()
    # если симптомов нет, но явно написано "аллергическая реакция" — оставим обобщённо
    if re.search(r"(?i)аллергическ\w+\s+реакци\w+", ctx or ""):
        return "аллергическая реакция"
    return None


def _find_drugs_in_ctx(ctx: str, *, max_hits: int = 3) -> List[str]:
    """Пытаемся вытащить названия препаратов из контекста по словарю drugs.txt."""
    s = (ctx or "").lower()
    if not s:
        return []

    # точка привязки: ближайшее к словам "реакц/непереносим" — обычно это виновный препарат
    anchor = None
    m = re.search(r"(?i)аллергическ\w+\s+реакци\w+|непереносимост\w+|гиперчувствительн\w+", s)
    if m:
        anchor = m.start()

    candidates: List[Tuple[int, str]] = []
    for d in load_drug_vocab():
        if len(d) < 4:
            continue
        pos = s.find(d)
        if pos == -1:
            continue
        if anchor is None:
            score = pos
        else:
            # штрафуем препараты, стоящие после "реакции" (часто это "замена на ...")
            after_penalty = 1000 if pos > anchor else 0
            score = abs(pos - anchor) + after_penalty
        candidates.append((score, d))

    candidates.sort(key=lambda x: x[0])
    return [d for _, d in candidates[:max_hits]]


def extract_allergies(
    text: str,
    *,
    include_weak_mentions: bool = True,
    max_items: int = 30,
) -> List[Dict[str, Any]]:
    """
    Схема:
    {substance, reaction, severity, source}
    """
    t = text or ""
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(substance: str, ctx: str, confidence: str) -> None:
        sub = _clean_substance(substance)
        if not sub:
            return
        key = sub.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "substance": sub,
                "reaction": _clean_reaction(ctx),
                "severity": _infer_severity(ctx),
                "source": f"confidence={confidence}; правила: аллергия: {(_norm_spaces(ctx)[:240])}",
            }
        )

    # 1) Strong: строки с заголовком
    for m in RE_ALLERGY_HEAD.finditer(t):
        ctx = m.group(0)
        body = m.group("body") or ""
        # явное отсутствие аллергий — не создаём записи
        if re.search(r"(?i)\b(нет\s+данн\w*\s+о\s+лекарственн\w+\s+аллерг\w+|аллерг\w+\s+нет|не\s+отягощ[её]н\w*)\b", body):
            continue
        # пытаемся вытащить "на/к <вещество>"
        mm = RE_ON.search(body)
        if mm:
            add(mm.group("sub"), ctx, "strong")
        else:
            # иначе берём как “список через запятые”
            parts = [p.strip() for p in re.split(r"[;,]", body) if p.strip()]
            for p in parts[:8]:
                add(p, ctx, "strong")
                if len(out) >= max_items:
                    return out

    if not include_weak_mentions:
        return out

    # 2) Weak: отдельные предложения
    for m in RE_ALLERGY_SENT.finditer(t):
        # Берём немного контекста СЛЕВА, иначе часто теряется виновный препарат:
        # "...Карбоплатин (аллергическая реакция)+Цисплатин..." -> матч начинается с "аллергическая"
        # и карбоплатин не попадает в ctx.
        a, b = m.span()
        ctx = t[max(0, a - 140): min(len(t), b + 220)]
        mm = RE_ON.search(ctx)
        if mm:
            add(mm.group("sub"), ctx, "weak")
        else:
            # Частый случай: "аллергическая реакция" внутри строки терапии без предлога "на".
            # В этом случае хвост предложения может быть мусорным ("реакция)+Цисплатин...").
            # Поэтому сначала пробуем найти препараты по словарю.
            drugs = _find_drugs_in_ctx(ctx)
            if drugs:
                # как минимум первый препарат фиксируем как culprit; остальные можно добавить отдельными записями
                add(drugs[0], ctx, "weak")
                # Остальные препараты в той же строке часто относятся к схеме лечения,
                # а не к аллергии (напр. "...карбоплатин (аллергическая реакция)+цисплатин...").
                # Поэтому по умолчанию НЕ добавляем их, чтобы не плодить ложноположительные.
            else:
                # fallback: 1–3 слова после "аллерг..." (как раньше)
                tail = re.sub(r"(?i).*?\b(аллерг\w+|непереносимост\w+)\b", "", ctx).strip()
                tail = tail.strip(" :\-—")
                if tail:
                    add(tail[:60], ctx, "weak")
        if len(out) >= max_items:
            return out

    return out