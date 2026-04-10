# src/coverage_layer.py
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from functools import lru_cache

# ============================================================
# COVERAGE LAYER (сырой слой)
# Цель: собрать "якоря" из текста (даты/числа/единицы/коды/препараты/и т.п.)
#       + позиции (span) в clean_text, чтобы дальше маппить и проверять полноту.
#
# ВАЖНО:
# - Этот слой НЕ "понимает" медицину и НЕ структурирует.
# - Он должен быть "high recall": лучше лишнее (с пометкой source), чем потерять.
# - span.end — EXCLUSIVE (как в Python slicing).
# - Все span относятся к clean_text.
# ============================================================


# -------------------------
# REGEXES (coverage v1.3)
# -------------------------

# Даты:
# - 12.03.2023 / 12-03-2023 / 12/03/2023
# - 12.03.25г / 12.03.25 г. / 12.03.25
# - 05.2023 (месяц-год)
# - 2023 (год)
# -------------------------
# Dates / numbers / units
# -------------------------
RE_DATE_DMY = re.compile(
    r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})[.\-/]((?:19|20)?\d{2})(?:\s*г\.?)?(?!\d)"
)
RE_DATE_MY = re.compile(
    r"(?<!\d)(\d{1,2})\.(\d{4})(?:\s*г\.?)?(?!\d)"
)
RE_DATE_Y = re.compile(
    r"(?<!\d)(19\d{2}|20\d{2})(?:\s*г\.?)?(?!\d)"
)

# Числа: 38.2 / 6,42 / 87 / 0.5 / 12
RE_NUMBER = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)")

# ICD-10 / МКБ-подобные: C34.1, D05, Z51.1 ...
RE_ICD10 = re.compile(r"\b[A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?\b")

# Единицы (MVP набор; расширяется словарём по мере надобности)
RE_UNIT = re.compile(
    r"(?<!\w)("
    r"мг/м2|мг/м²|мг/кг|г/л|ммоль/л|мкг/мл|мкг|мг|г|мл|л|%"
    r"|°C|°С|сод|гр|auc"
    r")\b",
    flags=re.IGNORECASE,
)

# -------------------------
# Medications (NEW MVP)
# -------------------------

# Стоп-слова, чтобы точно не было “рост/опухоль/лёгких” как medication
STOP_MED_WORDS = {
    "день","дни","раз","раза","курс","курса","курсов","цикл","циклы","линия","линии",
    "рост","продолженный","опухоль","опухоли","легкое","легких","очаг","очаги","очагов",
    "мтс","лимфоузлы","л/у","метастазы","прогрессирование","стабилизация",
}

# Коды/шифры (BCD-236, MK-3475, AZD9291 и т.п.)
RE_TRIAL_CODE = re.compile(r"(?i)\b[A-Z]{2,6}-\d{2,6}\b")

# “Онко-контекст” вокруг упоминания (не обязательно доза)
RE_ONCO_CONTEXT = re.compile(
    r"(?i)\b("
    r"хтт?|пхт|ит|тт|"
    r"линия|схема|режим|"
    r"q[1-4]w|еженедел|кажд(ые|ый)\s*\d+\s*(нед|дн)|"
    r"d\s*1|d\s*8|d1|d8|"
    r"в/в|per\s*os|п/о|"
    r"на фоне|отмена|отменен|отменена|неперенос|токсичност"
    r")\b"
)

# Суффиксы МНН (страховка на новые/опечатки; даёт кандидатов)
RE_ONCO_SUFFIX = re.compile(
    r"(?iu)\b[а-яё-]{4,}("
    r"умаб|ниб|циклиб|париб|текан|платин|таксел|рубицин|"
    r"фосфамид|мустин"
    r")\b"
)

# (Опционально) Если ты хочешь сохранить “текст схемы в скобках”,
# оставь это, но НЕ используй как источник medication — только как regimen_text.
RE_REGIMEN_PARENS = re.compile(
    r"(?i)\b(?:хтт?|пхт|ит|тт)\b[^()\n]{0,120}\(([^)\n]{3,240})\)"
)

# -------------------------
# Oncology anchors
# -------------------------

# TNM (минимально полезно для онко-ИБ как якорь)
RE_TNM = re.compile(r"(?i)\b[cpyra]?t\d+[abc]?\s*n\d+[abc]?(?:\s*m\d+[abc]?)?\b")

# Модальности (обследования) — якоря
RE_MODALITY = re.compile(r"(?i)\b(пэт-?кт|кт|мрт|узи|маммограф(?:ия|ии))\b")

# --- Biomarker anchors (oncology, universal) ---

# "Сильные" маркеры: почти не дают ложных срабатываний
RE_BIOMARKER_STRONG = re.compile(
    r"(?i)\b("
    r"pd-?l1|tmb|msi(?:-h)?|mss|"
    r"dmmr|pmmr|"
    r"cps|tps|"
    r"ki-?67|ki67|"
    r"brca1|brca2|"
    r"er\b|pr\b|her2|"
    r"ntrk1|ntrk2|ntrk3|ntrk\b|"
    r"egfr|alk|ros1|"
    r"kras|nras|braf|"
    r"pik3ca|pten|"
    r"fgfr2|fgfr\b|"
    r"kit|pdgfra|"
    r"idh1|idh2|mgmt|"
    r"cldn18\.?2|"
    r"ar\b|psa\b"
    r")\b"
)

# "Короткие/опасные" токены: MET / RET
RE_BIOMARKER_SHORT = re.compile(r"(?i)\b(met|ret)\b")

# Контекст, который делает "MET/RET" почти наверняка биомаркером
RE_BIOMARKER_CONTEXT = re.compile(
    r"(?i)\b("
    r"мутац|вариант|vaf|"
    r"реаранж|транслоц|фьюж|fusion|"
    r"амплифик|копи|copy|"
    r"экспресс|overexpress|"
    r"положит|отрицат|negativ|positiv|"
    r"ihc|игх|fish|n?gs|секвенир|"
    r"тест|статус|"
    r"del|ins|exon|экзон"
    r")\b"
)

# Нормализация значения маркера (в идеале текст уже нормализован cleaner'ом)
def _norm_biomarker_token(tok: str) -> str:
    t = tok.strip()
    t_up = t.upper()

    # унифицируем самые частые варианты
    if re.fullmatch(r"(?i)ki-?67|ki67", t):
        return "Ki67"
    if re.fullmatch(r"(?i)pd-?l1", t):
        return "PD-L1"
    if re.fullmatch(r"(?i)her2", t):
        return "HER2"
    if re.fullmatch(r"(?i)brca1", t):
        return "BRCA1"
    if re.fullmatch(r"(?i)brca2", t):
        return "BRCA2"
    if re.fullmatch(r"(?i)dmmr", t):
        return "dMMR"
    if re.fullmatch(r"(?i)pmmr", t):
        return "pMMR"
    if re.fullmatch(r"(?i)msi-h", t):
        return "MSI-H"
    if re.fullmatch(r"(?i)msi", t):
        return "MSI"
    if re.fullmatch(r"(?i)mss", t):
        return "MSS"
    if re.fullmatch(r"(?i)tmb", t):
        return "TMB"
    if re.fullmatch(r"(?i)cps", t):
        return "CPS"
    if re.fullmatch(r"(?i)tps", t):
        return "TPS"

    # гены/мишени оставляем в верхнем регистре
    if re.fullmatch(r"(?i)(egfr|alk|ros1|kras|nras|braf|pik3ca|pten|fgfr2|fgfr|kit|pdgfra|idh1|idh2|mgmt|met|ret|ntrk1|ntrk2|ntrk3|ntrk|ar|psa)", t):
        return t_up

    # CLDN18.2
    if re.fullmatch(r"(?i)cldn18\.?2", t):
        return "CLDN18.2"

    return t

@lru_cache(maxsize=1)
def _load_drug_dict() -> List[str]:
    p = Path("resources/drugs_onco_ru.txt")
    if not p.exists():
        return []
    items: List[str] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        items.append(t.lower())
    items.sort(key=len, reverse=True)
    return items

@lru_cache(maxsize=1)
def _drug_dict_regex() -> Optional[re.Pattern]:
    drugs = _load_drug_dict()
    if not drugs:
        return None
    alts = "|".join(re.escape(d) for d in drugs)
    return re.compile(rf"(?iu)\b(?:{alts})\b")

# -------------------------
# helpers
# -------------------------

def _context(text: str, start: int, end: int, left: int = 70, right: int = 70) -> str:
    a = max(0, start - left)
    b = min(len(text), end + right)
    return text[a:b]


def _sha256(s: str) -> str:
    h = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{h}"


def _make_id(i: int) -> str:
    return f"e{i:06d}"


def _add_entity(
    out: List[Dict[str, Any]],
    *,
    eid: str,
    typ: str,
    value: str,
    span: Tuple[int, int],
    text: str,
    source: str,
    norm: Any = None,
    attrs: Optional[Dict[str, Any]] = None,
) -> None:
    s, e = span
    a: Dict[str, Any] = attrs or {}

    out.append(
        {
            "id": eid,
            "type": typ,
            "value": value,
            "norm": norm,
            "span": {"start": s, "end": e},  # end EXCLUSIVE
            "context": _context(text, s, e),
            "source": source,
            "confidence": a.get("confidence"),
            "negated": a.get("negated"),
            "attrs": a,
        }
    )


def _iter_matches(rx: re.Pattern, text: str) -> Iterable[Tuple[str, int, int]]:
    for m in rx.finditer(text):
        yield (m.group(0), m.start(), m.end())


def _contains_span(entities: List[Dict[str, Any]], s: int, e: int, typ: str) -> bool:
    # True если (s,e) полностью лежит внутри уже добавленной сущности typ
    for ent in entities:
        if ent.get("type") != typ:
            continue
        sp = ent.get("span") or {}
        ss = sp.get("start")
        ee = sp.get("end")
        if isinstance(ss, int) and isinstance(ee, int) and ss <= s and e <= ee:
            return True
    return False


def _dedupe_entities(entities: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    # Дедуп по (type, start, end, value)
    seen = set()
    out: List[Dict[str, Any]] = []
    removed = 0
    for e in entities:
        sp = e.get("span") or {}
        key = (e.get("type"), sp.get("start"), sp.get("end"), e.get("value"))
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        out.append(e)
    return out, removed


# ============================================================
# BUILD
# ============================================================

def build_coverage_layer(
    *,
    raw_text: str,
    clean_text: str,
    cleaner_version: str = "v1.3",
    lang: str = "ru",
    source_type: str = "text",
) -> Dict[str, Any]:
    """
    Coverage JSON:
    - span.end is EXCLUSIVE (python slicing)
    - spans refer to clean_text (это важно!)
    """

    created_at = datetime.now(timezone.utc).isoformat()

    entities: List[Dict[str, Any]] = []
    i = 1

    # 1) Dates (сначала, чтобы можно было не путать месяц-год/год с частью полной даты)
    for val, s, e in _iter_matches(RE_DATE_DMY, clean_text):
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="date",
            value=val,
            span=(s, e),
            text=clean_text,
            source="regex:date_dmy",
        )
        i += 1

    for val, s, e in _iter_matches(RE_DATE_MY, clean_text):
        # не дублируем, если это часть dd.mm.yyyy или dd.mm.yy
        if _contains_span(entities, s, e, typ="date"):
            continue
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="date",
            value=val,
            span=(s, e),
            text=clean_text,
            source="regex:date_my",
        )
        i += 1

    for val, s, e in _iter_matches(RE_DATE_Y, clean_text):
        # не дублируем год, если он внутри уже найденной даты/месяц-год
        if _contains_span(entities, s, e, typ="date"):
            continue
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="date",
            value=val,
            span=(s, e),
            text=clean_text,
            source="regex:date_y",
        )
        i += 1

    # 2) Numbers (ловим ВСЕ числа; маппинг потом разберёт, что к чему)
    for val, s, e in _iter_matches(RE_NUMBER, clean_text):
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="number",
            value=val,
            span=(s, e),
            text=clean_text,
            source="regex:number",
        )
        i += 1

    # 3) Units
    for val, s, e in _iter_matches(RE_UNIT, clean_text):
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="unit",
            value=val,
            span=(s, e),
            text=clean_text,
            source="regex:unit",
        )
        i += 1

    # 4) Diagnosis codes (ICD-like)
    for val, s, e in _iter_matches(RE_ICD10, clean_text):
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="diagnosis_code",
            value=val,
            span=(s, e),
            text=clean_text,
            source="regex:icd10",
        )
        i += 1

    # 5) TNM anchors
    for val, s, e in _iter_matches(RE_TNM, clean_text):
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="tnm",
            value=val,
            span=(s, e),
            text=clean_text,
            source="regex:tnm",
        )
        i += 1

    # 6) Biomarkers anchors (oncology, universal)

    def _window(text: str, s: int, e: int, n: int = 80) -> str:
        a = max(0, s - n)
        b = min(len(text), e + n)
        return text[a:b]

    # 6.1 Сильные маркеры — берём всегда
    for val, s, e in _iter_matches(RE_BIOMARKER_STRONG, clean_text):
        norm = _norm_biomarker_token(val)
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="biomarker",
            value=val,
            norm=norm,
            span=(s, e),
            text=clean_text,
            source="regex:biomarker_strong",
            attrs={"kind": "strong"},
        )
        i += 1

    # 6.2 Короткие маркеры MET/RET — только если есть “биомаркерный” контекст рядом
    for val, s, e in _iter_matches(RE_BIOMARKER_SHORT, clean_text):
        ctx = _window(clean_text, s, e, n=80)
        if not RE_BIOMARKER_CONTEXT.search(ctx):
            # это почти наверняка не биомаркер → пропускаем
            continue

        norm = _norm_biomarker_token(val)
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="biomarker",
            value=val,
            norm=norm,
            span=(s, e),
            text=clean_text,
            source="regex:biomarker_short_ctx",
            attrs={"kind": "short_ctx"},
        )
        i += 1

    # 7) Modalities
    for val, s, e in _iter_matches(RE_MODALITY, clean_text):
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="modality",
            value=val,
            span=(s, e),
            text=clean_text,
            source="regex:modality",
        )
        i += 1

    # 8) Medications — MVP (dict + trial codes + suffix with confidence)

    drug_rx = _drug_dict_regex()

    # A) Словарь (high)
    if drug_rx is not None:
        for m in drug_rx.finditer(clean_text):
            val = m.group(0)
            if val.lower() in STOP_MED_WORDS:
                continue
            s, e = m.start(), m.end()
            _add_entity(
                entities,
                eid=_make_id(i),
                typ="medication",
                value=val,
                norm=val.lower(),
                span=(s, e),
                text=clean_text,
                source="dict:mnn",
                attrs={"evidence": ["dict_match"], "confidence": "high"},
            )
            i += 1

    # B) Коды клинических исследований (high)
    for m in RE_TRIAL_CODE.finditer(clean_text):
        val = m.group(0)
        s, e = m.start(), m.end()
        _add_entity(
            entities,
            eid=_make_id(i),
            typ="medication",
            value=val,
            norm=val.upper(),
            span=(s, e),
            text=clean_text,
            source="regex:trial_code",
            attrs={"evidence": ["trial_code"], "confidence": "high"},
        )
        i += 1

    # C) Суффиксы (medium/low) — страховка на новые МНН/опечатки
    for m in RE_ONCO_SUFFIX.finditer(clean_text):
        val = m.group(0)
        vlow = val.lower()
        if vlow in STOP_MED_WORDS:
            continue

        s, e = m.start(), m.end()
        ctx = _window(clean_text, s, e, n=80)

        evidence = ["suffix"]
        conf = "low"
        if RE_ONCO_CONTEXT.search(ctx):
            conf = "medium"
            evidence.append("onco_context")

        _add_entity(
            entities,
            eid=_make_id(i),
            typ="medication",
            value=val,
            norm=vlow,
            span=(s, e),
            text=clean_text,
            source="regex:suffix",
            attrs={"evidence": evidence, "confidence": conf},
        )
        i += 1

    # 10) Дедуп (важно для стабильных проверок полноты)
    entities, removed = _dedupe_entities(entities)

    # Views (типы -> список id)
    views: Dict[str, List[str]] = {}
    for ent in entities:
        views.setdefault(ent["type"], []).append(ent["id"])

    coverage = {
        "counts": {k: len(v) for k, v in views.items()},
        "notes": [],
        "dedup_removed": removed,
    }

    doc = {
        "meta": {
            "text_hash": _sha256(clean_text),
            "cleaner_version": cleaner_version,
            "created_at": created_at,
            "lang": lang,
            "length": len(clean_text),
            "source_type": source_type,
        },
        "text": {
            "raw": raw_text,
            "clean": clean_text,
        },
        "entities": entities,
        "views": views,
        "coverage": coverage,
        "edges": [],  # на этапе coverage обычно пусто; связи строятся на mapping-этапе
    }
    return doc


# ============================================================
# QUALITY CHECKS (coverage-aware)
# ============================================================

def quality_check_coverage(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Идея:
    - "ok" здесь = базовая целостность + грубая проверка покрытия по тем сущностям,
      которые мы ДОЛЖНЫ уметь извлекать (по своим же regex / каналам).
    - Дополнительно выдаём "warnings" (не фатально), чтобы видеть шум/дыры.
    """
    issues: List[str] = []
    warnings: List[str] = []

    text_obj = doc.get("text")
    text = text_obj.get("clean", "") if isinstance(text_obj, dict) else ""
    ents = doc.get("entities") or []
    if not isinstance(ents, list):
        return {"ok": False, "issues": ["entities is not a list"], "warnings": []}

    # 1) id unique
    ids = [e.get("id") for e in ents if isinstance(e, dict)]
    if len(ids) != len(set(ids)):
        issues.append("Дубли entity.id")

    # 2) span bounds and start<end
    for e in ents:
        if not isinstance(e, dict):
            continue
        sp = e.get("span") or {}
        s = sp.get("start")
        en = sp.get("end")
        if not isinstance(s, int) or not isinstance(en, int):
            issues.append(f"Некорректный span у {e.get('id')}")
            continue
        if not (0 <= s < en <= len(text)):
            issues.append(f"Span вне границ текста у {e.get('id')}: [{s},{en}) len={len(text)}")

    # 3) duplicates by (type,start,end,value) — на всякий случай (после дедупа не должно)
    seen = set()
    for e in ents:
        if not isinstance(e, dict):
            continue
        sp = e.get("span") or {}
        key = (e.get("type"), sp.get("start"), sp.get("end"), e.get("value"))
        if key in seen:
            issues.append(f"Дубль сущности по ключу {key}")
            break
        seen.add(key)

    # -----------------------------
    # Coverage sanity checks (warnings)
    # -----------------------------
    meds = [e for e in ents if isinstance(e, dict) and e.get("type") == "medication"]
    bios = [e for e in ents if isinstance(e, dict) and e.get("type") == "biomarker"]

    # A) Если есть коды КИ в тексте, но мы не извлекли их как medication
    if text and RE_TRIAL_CODE.search(text):
        if not any(e.get("source") == "regex:trial_code" for e in meds):
            warnings.append("В тексте найдены коды КИ (trial code), но не извлечены как medication (regex:trial_code)")

    # B) Если есть суффиксные кандидаты в онко-контексте, но нет ни одного suffix-лекарства
    # (это мягкая проверка: не всегда должен быть, но часто сигналит о дыре)
    if text:
        found_suffix_in_context = False
        for m in RE_ONCO_SUFFIX.finditer(text):
            s, en = m.start(), m.end()
            a = max(0, s - 80)
            b = min(len(text), en + 80)
            ctx = text[a:b]
            if RE_ONCO_CONTEXT.search(ctx):
                found_suffix_in_context = True
                break

        if found_suffix_in_context and not any(e.get("source") == "regex:suffix" for e in meds):
            warnings.append("Есть 'препаратоподобные' слова (суффиксы) в онко-контексте, но канал regex:suffix ничего не извлёк")

    # C) Биомаркеры: если в тексте есть сильные якоря, а сущностей biomarker нет
    if text and RE_BIOMARKER_STRONG.search(text):
        if not bios:
            warnings.append("В тексте есть биомаркеры (strong anchors), но сущности biomarker не извлечены")

    # D) MET/RET: если встречаются, но рядом нет контекста и всё равно извлечены — предупредим о возможном шуме
    # (только warning, не issue)
    if text:
        for e in bios:
            val = (e.get("value") or "").lower()
            if val in {"met", "ret"}:
                sp = e.get("span") or {}
                s = sp.get("start", 0)
                en = sp.get("end", 0)
                a = max(0, int(s) - 80)
                b = min(len(text), int(en) + 80)
                ctx = text[a:b]
                if not RE_BIOMARKER_CONTEXT.search(ctx):
                    warnings.append(f"Биомаркер '{val.upper()}' извлечён без поддерживающего контекста — возможно шум")

    return {"ok": len(issues) == 0, "issues": issues, "warnings": warnings}

    # 4) Coverage sanity: compare regex matches to entity counts (по тем же regex)
    def _count_regex(rx: re.Pattern) -> int:
        return sum(1 for _ in rx.finditer(text))

    def _count_entities(typ: str) -> int:
        return sum(1 for e in ents if isinstance(e, dict) and e.get("type") == typ)

    # numbers: должно совпасть 1-в-1 (мы добавляем все матчи RE_NUMBER)
    n_numbers = _count_regex(RE_NUMBER)
    n_number_entities = _count_entities("number")
    if n_numbers != n_number_entities:
        issues.append(f"Несовпадение числа чисел: regex={n_numbers}, entities(number)={n_number_entities}")

    # dates: мы добавляем все матчи RE_DATE_DMY + (RE_DATE_MY/RE_DATE_Y, если не внутри уже найденного date)
    # поэтому тут делаем более мягкую проверку: если в тексте есть DMY, то date-entities обязаны быть >0
    n_dmy = _count_regex(RE_DATE_DMY)
    n_date_entities = _count_entities("date")
    if n_dmy > 0 and n_date_entities == 0:
        issues.append(f"В тексте есть даты dd.mm.yy(yy) (≈{n_dmy}), но date-entities=0")

    # units: если есть единицы по regex, а unit=0 — это почти наверняка ошибка пайплайна
    n_units = _count_regex(RE_UNIT)
    n_unit_entities = _count_entities("unit")
    if n_units > 0 and n_unit_entities == 0:
        issues.append(f"В тексте есть единицы измерения (≈{n_units}), но unit-entities=0")

    # icd10: мягко
    n_icd = _count_regex(RE_ICD10)
    n_icd_entities = _count_entities("diagnosis_code")
    if n_icd > 0 and n_icd_entities == 0:
        issues.append(f"В тексте есть ICD/МКБ-подобные коды (≈{n_icd}), но diagnosis_code=0")

    # meds: если есть скобочные схемы, а meds из скобок нет — предупреждение (не всегда обязано)
    n_reg_parens = _count_regex(RE_REGIMEN_PARENS)
    n_meds_parens = sum(
        1
        for e in ents
        if isinstance(e, dict)
        and e.get("type") == "medication"
        and str(e.get("source", "")).startswith("regex:med_in_regimen_parens")
    )
    if n_reg_parens > 0 and n_meds_parens == 0:
        warnings.append(
            "Найдены схемы терапии в скобках после ХТ/ПХТ/ХТТ, но препараты внутри скобок не извлечены "
            f"(regimen_parens≈{n_reg_parens}). Возможно, надо расширить RE_DRUG_TOKEN/фильтры."
        )

    # шум: если эвристика 'назначен' извлекла слишком много коротких/служебных слов — предупреждение
    meds = [e for e in ents if isinstance(e, dict) and e.get("type") == "medication"]
    suspicious = [e for e in meds if str(e.get("value", "")).lower() in {"день", "раз", "раза", "курс"}]
    if suspicious:
        warnings.append(f"Подозрительные medication-токены (служебные слова): {len(suspicious)} шт. Проверь фильтры.")

    return {"ok": len(issues) == 0, "issues": issues, "warnings": warnings}


# ============================================================
# CLI
# ============================================================

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    """
    Пример:
      python -m src.coverage_layer --text cases/source.txt --out cases/coverage.json --report cases/coverage_report.json --already_clean
    """
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="Путь к txt (сырой или уже очищенный)")
    ap.add_argument("--out", required=True, help="Куда сохранить coverage.json")
    ap.add_argument("--report", required=True, help="Куда сохранить coverage_report.json")
    ap.add_argument("--cleaner_version", default="v1.3")
    ap.add_argument("--source_type", default="text")
    ap.add_argument(
        "--already_clean",
        action="store_true",
        help="Укажи, если входной txt уже прошёл очистку (cleaner/normalize_med_text)",
    )
    args = ap.parse_args()

    text_path = Path(args.text)
    out_path = Path(args.out)
    report_path = Path(args.report)

    raw_text = _read_text(text_path)

    # clean_text: либо уже готовый, либо прогоняем через cleaner
    if args.already_clean:
        clean_text = raw_text
    else:
        # ВАЖНО: подставь сюда реальную функцию из normalize_med_text.py,
        # которая делает твою нормализацию (HER2/neu -> HER2, E G F R -> EGFR, PD L 1 -> PD-L1, и т.д.)
        try:
            from normalize_med_text import clean_text as _clean_text  # <-- если у тебя функция называется иначе, поменяй тут
            clean_text = _clean_text(raw_text)
        except Exception:
            # Фолбек: не падаем, но честно работаем как раньше
            clean_text = raw_text

    doc = build_coverage_layer(
        raw_text=raw_text,
        clean_text=clean_text,
        cleaner_version=args.cleaner_version,
        lang="ru",
        source_type=args.source_type,
    )
    report = quality_check_coverage(doc)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    _write_json(out_path, doc)
    _write_json(report_path, report)

    # чуть-чуть удобного stdout
    counts = doc.get("coverage", {}).get("counts", {})
    print("coverage counts:", counts)
    print("dedup removed:", doc.get("coverage", {}).get("dedup_removed"))
    print("report ok:", report.get("ok"))
    if report.get("issues"):
        print("issues:")
        for x in report["issues"]:
            print(" -", x)
    if report.get("warnings"):
        print("warnings:")
        for x in report["warnings"]:
            print(" -", x)


if __name__ == "__main__":
    main()