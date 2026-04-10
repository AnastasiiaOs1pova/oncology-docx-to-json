from __future__ import annotations

import json
import re
from datetime import date
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import ollama  # type: ignore
except Exception:  # pragma: no cover
    ollama = None  # type: ignore
from jsonschema import Draft202012Validator

from .extract_text import extract_text
from .normalize_med_text import apply_replacements
from .coverage_layer import build_coverage_layer, quality_check_coverage
from .rules_to_case import build_case_from_rules, extract_biomarkers
from .rules.patient_context import fill_patient_context_inplace


# =============================
# Post-processing (качество данных)
# =============================

RE_DATE_DMY = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s*(?:г\.?)*\s*$")
RE_DATE_MY  = re.compile(r"^\s*(\d{1,2})\.(\d{4})\s*(?:г\.?)*\s*$")

def date_to_iso_like(s: str) -> str:
    s = (s or "").strip()
    m = RE_DATE_DMY.match(s)
    if m:
        dd, mm, yy = m.groups()
        yy = yy if len(yy) == 4 else ("20" + yy)
        return f"{yy}-{int(mm):02d}-{int(dd):02d}"
    m = RE_DATE_MY.match(s)
    if m:
        mm, yy = m.groups()
        return f"{yy}-{int(mm):02d}"
    return s
def _date_key(s: Optional[str]) -> Optional[Tuple[int,int,int]]:
    if not s:
        return None
    # поддержка YYYY-MM и YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", s)
    if not m:
        return None
    y = int(m.group(1)); mo = int(m.group(2)); d = int(m.group(3) or 1)
    return (y, mo, d)

def _status_norm_from_text(s: str) -> str:
    t = (s or "").lower()
    neg = bool(re.search(r"(не\s+обнаруж|не\s+выявл|отрицат|wild\s*type|\bwt\b|no\s+mutation|not\s+detected)", t))
    pos = bool(re.search(r"(обнаруж|выявл|положит|detected|mutation|mutat|fusion|реаранж|амплифик|amplif)", t))
    if neg and not pos:
        return "negative"
    if pos and not neg:
        return "positive"
    if neg and pos:
        # в фразах типа “мутации ... не обнаружены” оба могут встретиться -> negative важнее
        return "negative"
    return "unknown"

def normalize_biomarkers_inplace(data: Dict[str, Any], text: str) -> None:
    bms = data.get("biomarkers")
    if not isinstance(bms, list):
        return

    fixed: List[Dict[str, Any]] = []
    seen = set()

    for bm in bms:
        if not isinstance(bm, dict):
            continue
        std = (bm.get("name_std") or "").lower().strip()
        val = bm.get("value")
        src = bm.get("source") or ""
        # 1) вытаскиваем дату из source, если там есть “МГИ от dd.mm.yyyy”
        if std.startswith("brca") or std in {"mss","tmb","msi","dmmr","pmmr"}:
            mm = re.search(r"(?:мги|молекул\w*)\s+от\s*(\d{2}\.\d{2}\.\d{4})", src, flags=re.I)
            if mm:
                bm["date"] = date_to_iso_like(mm.group(1))

        # 3) dedupe по (std, value, date)
        key = (std, str(bm.get("value") or ""), str(bm.get("date") or ""))
        if key in seen:
            continue
        seen.add(key)
        fixed.append(bm)

    # 4) fallback: MSS/TMB из блока “МГИ от ...”
    # (если rules не вытащили из-за границ safe-zone)
    if not any((d.get("name_std") == "mss") for d in fixed):
        for m in re.finditer(r"мги\s+от\s*(\d{2}\.\d{2}\.\d{4})\s*:\s*([^\n]{0,220})", text, flags=re.I):
            d = date_to_iso_like(m.group(1))
            tail = m.group(2)
            if re.search(r"\bMSS\b", tail, flags=re.I):
                fixed.append({
                    "name_raw":"MSS","name_std":"mss","value":"MSS",
                    "unit": None,"date": d,"method": None,
                    "source": f"правила:fallback МГИ: {tail.strip()[:220]}"
                })
            mt = re.search(r"\bTMB\b\s*([0-9]+(?:[\.,][0-9]+)?)", tail, flags=re.I)
            if mt:
                fixed.append({
                    "name_raw":"TMB","name_std":"tmb","value": mt.group(1).replace(',','.'),
                    "unit": None,"date": d,"method": None,
                    "source": f"value={mt.group(1)}; правила:fallback МГИ: {tail.strip()[:220]}"
                })
            break

    data["biomarkers"] = fixed

def split_regimen_to_drugs(regimen_name: str) -> List[Dict[str, Any]]:
    s = (regimen_name or "").strip()
    if not s:
        return []
    # нормализация разделителей
    s2 = re.sub(r"\s*\+\s*", "+", s)
    parts = []
    for p in s2.split("+"):
        p = p.strip()
        if not p:
            continue
        # альтернативы через /
        alts = [x.strip() for x in p.split("/") if x.strip()]
        if len(alts) == 1:
            parts.append((alts[0], None))
        else:
            # группа альтернатив
            for a in alts:
                parts.append((a, 1))
    out = []
    for raw, alt_group in parts:
        # убрать дозы/единицы/скобки
        t = re.sub(r"\([^\)]*\)", " ", raw)
        t = re.sub(r"\d+[\d\s\./-]*(?:мг/м2|мг|г|мкг|meq|auc\d+)?", " ", t, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip()
        # привести творительный падеж (очень грубо для MVP)
        t_low = t.lower()
        for suf in ["ом", "ем", "ой", "ою", "ами", "ями"]:
            if t_low.endswith(suf) and len(t_low) > 5:
                t_low = t_low[: -len(suf)]
                break
        std = t_low.strip("- ").replace("  "," ")
        out.append({"raw": raw, "std": std or t_low, "alternative_group": alt_group})
    return out

def enrich_treatment_history_inplace(data: Dict[str, Any]) -> None:
    th = data.get("treatment_history")
    if not isinstance(th, list):
        return
    for item in th:
        if not isinstance(item, dict):
            continue
        rn = item.get("regimen_name") or ""
        item["drugs"] = split_regimen_to_drugs(rn)

def normalize_progression_dates_inplace(data: Dict[str, Any]) -> None:
    """Дедуплицирует и сортирует diagnoses[0].dates.progression_dates, не добавляя новых полей (чтобы пройти схему)."""
    diags = data.get("diagnoses") or []
    if not (isinstance(diags, list) and diags and isinstance(diags[0], dict)):
        return
    dates_obj = diags[0].get("dates") or {}
    if not isinstance(dates_obj, dict):
        return
    prog = dates_obj.get("progression_dates") or []
    if not isinstance(prog, list) or not prog:
        return

    uniq = []
    seen = set()
    for d in prog:
        if not d or not isinstance(d, str):
            continue
        if d in seen:
            continue
        seen.add(d)
        uniq.append(d)

    uniq.sort(key=lambda x: _date_key(x) or (9999, 12, 31))
    dates_obj["progression_dates"] = uniq

def add_progression_links_to_quality_gate(data: Dict[str, Any]) -> None:
    """Пишет диагностическое описание привязки 'прогрессирование -> линия' в quality_gate.issues, не меняя схему."""
    diags = data.get("diagnoses") or []
    if not (isinstance(diags, list) and diags and isinstance(diags[0], dict)):
        return
    prog = ((diags[0].get("dates") or {}).get("progression_dates") or [])
    if not isinstance(prog, list) or not prog:
        return

    th = data.get("treatment_history") or []
    if not isinstance(th, list) or not th:
        return
    th_sorted = sorted([x for x in th if isinstance(x, dict) and x.get("line") is not None],
                       key=lambda z: z.get("line"))

    links = []
    for d in prog:
        dk = _date_key(d)
        if not dk:
            continue
        following = None
        for ln in th_sorted:
            sk = _date_key(ln.get("start_date"))
            if sk and sk > dk:
                following = ln
                break
        if not following:
            continue
        idx = th_sorted.index(following)
        if idx == 0:
            continue
        preceding = th_sorted[idx - 1]
        links.append(f"{d} -> line {preceding.get('line')} (до), затем line {following.get('line')} (после)")

    if not links:
        return

    qg = data.setdefault("quality_gate", {}) if isinstance(data, dict) else {}
    if isinstance(qg, dict):
        issues = qg.setdefault("issues", [])
        if isinstance(issues, list):
            issues.append("Привязка прогрессирования к линиям (эвристика): " + "; ".join(links))

TEMPLATE_PATH = Path("examples/case_empty.json")
SCHEMA_PATH = Path("schemas/container.schema.json")


# ============================================================
# 1) LLM PROMPTS
# ============================================================
SYSTEM_TIMELINE_EVENTS = """Ты извлекаешь хронологические события из медицинской выписки.

КРИТИЧЕСКИ ВАЖНО:
- Верни ТОЛЬКО JSON. Никаких пояснений, markdown, текста.
- НЕ придумывай факты, даты, препараты. Только то, что явно есть в тексте.
- Каждое событие обязано содержать ДОСЛОВНУЮ цитату (text_snippet) из исходного текста.
- Событие добавляй ТОЛЬКО если в цитате есть явная дата/период: DD.MM.YYYY, MM.YYYY, YYYY, или "с ... по ...", "от ...".
- Если период "с ... по ..." — верни ДВА события: therapy_start и therapy_end (с одной и той же цитатой).

ТИПЫ СОБЫТИЙ (event_type):
- therapy_start
- therapy_end
- progression
- imaging
- surgery
- radiotherapy
- diagnosis
- recommendation
- other

Формат:
{
  "events": [
    {
      "date": "YYYY-MM-DD|YYYY-MM|YYYY",
      "date_precision": "day|month|year|unknown",
      "event_type": "therapy_start|therapy_end|progression|imaging|surgery|radiotherapy|diagnosis|recommendation|other",
      "text_snippet": "дословная цитата 1-3 строки, где есть дата",
      "confidence": 0.0
    }
  ]
}

ПРАВИЛА ДЛЯ date:
- Если в тексте DD.MM.YYYY -> date = YYYY-MM-DD, date_precision=day
- Если MM.YYYY -> date = YYYY-MM, date_precision=month
- Если только YYYY -> date = YYYY, date_precision=year
- Если дата указана как "01.2022" -> YYYY-MM
- Если дата не извлекается однозначно -> не добавляй событие.

Верни события в порядке появления в тексте (НЕ сортируй).
"""

SYSTEM_MISSING_THERAPY = """Ты — ревизор (LLM reviewer) по противоопухолевому лечению.

Вход:
1) ТЕКСТ истории болезни (строка).
2) Список regimen, которые уже извлечены правилами (found_regimens).

Твоя задача:
- Найти В ТЕКСТЕ ВСЕ упоминания противоопухолевого лечения (ХТ/ИО/таргет/ГТ/КИ), даже если это:
  - уже есть в found_regimens,
  - "планируется/рекомендовано",
  - или непонятно, было ли начато.
- НИЧЕГО не добавляй сам в case.json — ты только составляешь список находок для ручной проверки.

КРИТИЧЕСКИ ВАЖНО (anti-hallucination):
- НЕ выдумывай. Если нет явного подтверждения в тексте — не добавляй.
- Каждый пункт ОБЯЗАН иметь quote — короткую цитату (1–2 строки),
  которая встречается в тексте БУКВАЛЬНО (подстрокой). Без quote — пункт не возвращай.
- Не подменяй смысл: не превращай "планируется/рекомендовано" в "проведено".
- Не добавляй диагностику/обследования/операции/лучевую (если это не системная терапия).
- Поддерживающая терапия (антиеметики, Г-КСФ, антибиотики и т.п.) — НЕ нужна, только противоопухолевая.

Формат ответа (строго, только JSON):
{
  "found": [
    {
      "regimen": null,
      "kind": "chemo|immunotherapy|targeted|hormone|trial|other",
      "mention_type": "administered|planned|recommended|unclear",
      "quote": null,
      "confidence": "high|medium|low",
      "date_hint": null,
      "line_hint": null,
      "note": null
    }
  ]
}

Правила:
- regimen: кратко схема/препарат(ы) как в тексте (пример: "FOLFOX", "пембролизумаб", "энкорафениб+цетуксимаб").
- quote: 1–2 строки из текста, где видно это упоминание терапии.
- mention_type:
  - administered = в тексте явно "получала/проведено/назначено и начато/проведена терапия/проведен курс"
  - planned = "планируется/запланировано" без признака начала
  - recommended = "рекомендовано" / "показано" без признака начала
  - unclear = упоминание есть, но статус неясен
- date_hint/line_hint: заполни только если прямо видно рядом (например "09.2021", "1 линия"), иначе null.
- Верни все находки без дублей.
""" 


# ============================================================
# 2) IO / UTILS
# ============================================================

RE_DOB = re.compile(r"(?:Дата\s*рождения|д/р|DOB)\s*[:\-]\s*(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)

def ddmmyyyy_to_iso(s: str) -> Optional[str]:
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", (s or "").strip())
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    try:
        return date(yyyy, mm, dd).isoformat()
    except ValueError:
        return None

def extract_dob(text: str) -> Optional[str]:
    head = (text or "")[:5000]
    m = RE_DOB.search(head)
    return ddmmyyyy_to_iso(m.group(1)) if m else None

def infer_sex(text: str) -> Optional[str]:
    head = (text or "")[:5000]
    m = re.search(r"\bпол\s*[:\-]\s*(жен|женский|муж|мужской)\b", head, re.IGNORECASE)
    if m:
        return "F" if m.group(1).lower().startswith("жен") else "M"
    if re.search(r"\bпациентка\b", text or "", re.IGNORECASE):
        return "F"
    if re.search(r"\bпациент\b", text or "", re.IGNORECASE):
        return "M"
    return None
def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def strip_code_fences(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        parts = s.split("```")
        if len(parts) >= 3:
            s = parts[1].strip()
            if s.lower().startswith("json"):
                s = s[4:].strip()
    return (s or "").strip()


def extract_first_json_object(s: str) -> str:
    s = strip_code_fences(s).strip()
    if not s:
        return s
    if s.lstrip().startswith("{") and s.rstrip().endswith("}"):
        return s

    start = s.find("{")
    if start == -1:
        return s

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
    return s[start:]


def fix_invalid_backslashes(s: str) -> str:
    out: List[str] = []
    in_str = False
    esc = False
    i = 0
    while i < len(s):
        ch = s[i]
        if not in_str:
            if ch == '"':
                in_str = True
            out.append(ch)
            i += 1
            continue

        if esc:
            esc = False
            out.append(ch)
            i += 1
            continue

        if ch == "\\":
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if nxt in ['"', "\\", "/", "b", "f", "n", "r", "t", "u"]:
                out.append(ch)
                esc = True
            else:
                out.append("\\\\")
            i += 1
            continue

        if ch == '"':
            in_str = False
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def looks_like_json_object(s: str) -> bool:
    s = strip_code_fences(s).strip()
    return s.startswith("{") and ("}" in s)


def parse_json_strict(raw: str) -> Dict[str, Any]:
    raw = extract_first_json_object(raw)
    if not raw.strip():
        raise json.JSONDecodeError("empty", raw, 0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fixed = fix_invalid_backslashes(raw)
        return json.loads(fixed)


def validate_or_raise(data: Dict[str, Any], schema: Dict[str, Any]) -> None:
    v = Draft202012Validator(schema)
    errs = sorted(v.iter_errors(data), key=lambda e: list(e.path))
    if errs:
        lines = ["JSON не прошёл схему:"]
        for e in errs[:120]:
            lines.append(f"- {list(e.path)}: {e.message}")
        raise ValueError("\n".join(lines))

def ollama_extract_timeline(*, text: str, model: str, out_dir: Path | None = None) -> Dict[str, Any]:
    raw = ollama_extract(model=model, system=SYSTEM_TIMELINE_EVENTS, user_prompt=text)

    if out_dir is not None:
        (out_dir / "llm_timeline_raw.txt").write_text(raw, encoding="utf-8")

    rawj = extract_first_json_object(raw)
    if not looks_like_json_object(rawj):
        return {"events": [], "error": "not_json"}

    try:
        doc = parse_json_strict(rawj)
    except Exception as e:
        return {"events": [], "error": f"json_parse_error: {e}"}

    events = doc.get("events")
    if not isinstance(events, list):
        return {"events": [], "error": "events_not_list"}

    out: List[Dict[str, Any]] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        dt = e.get("date")
        prec = e.get("date_precision")
        et = e.get("event_type")
        snip = e.get("text_snippet")
        conf = e.get("confidence", 0.0)

        if not isinstance(dt, str) or not dt.strip():
            continue
        if prec not in ("day", "month", "year", "unknown"):
            prec = "unknown"
        if et not in ("therapy_start","therapy_end","progression","imaging","surgery","radiotherapy","diagnosis","recommendation","other"):
            et = "other"
        if not isinstance(snip, str) or not snip.strip():
            continue
        try:
            conf_f = float(conf)
        except Exception:
            conf_f = 0.0
        conf_f = max(0.0, min(1.0, conf_f))

        out.append({
            "date": dt.strip(),
            "date_precision": prec,
            "event_type": et,
            "text_snippet": snip.strip(),
            "confidence": conf_f,
        })

    return {"events": out, "error": None}


def slice_segments(text: str, seg_doc: Dict[str, Any]) -> Dict[str, str]:
    """Возвращает dict: имя_сегмента -> текст сегмента (склейка, если сегментов одного типа несколько)."""
    segs = (seg_doc or {}).get("segments") or []
    buckets: Dict[str, List[str]] = {}
    for s in segs:
        if not isinstance(s, dict):
            continue
        seg = s.get("segment")
        a = s.get("start_char")
        b = s.get("end_char")
        if not (isinstance(seg, str) and isinstance(a, int) and isinstance(b, int)):
            continue
        chunk = text[a:b].strip()
        if chunk:
            buckets.setdefault(seg, []).append(chunk)

    return {k: "\n\n".join(v).strip() for k, v in buckets.items() if v}
def ollama_extract(model: str, system: str, user_prompt: str) -> str:
    resp = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "temperature": 0,
            "num_ctx": 8192,
            "stop": ["\n\n###", "\n###", "\n\nКраткий", "Краткий анализ", "Рекомендации:", "\nЕсли у вас есть"],
        },
    )
    return (resp.get("message", {}) or {}).get("content", "") or ""


def select_relevant_text(text: str, max_chars: int = 22000) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for ln in t.split("\n"):
        ln = " ".join(ln.strip().split())
        if ln:
            lines.append(ln)
    out = "\n".join(lines).strip()
    return out[:max_chars].rstrip() if len(out) > max_chars else out


# ============================================================
# 3) RULES: DATES / PROGRESSION
# ============================================================

RE_RANGE = re.compile(
    r"(?:с|c)\s*(?P<start>\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{4}|\d{4})\s*(?:г\.?)?\s*"
    r"(?:по|-|—)\s*"
    r"(?P<end>\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{4}|\d{4})\s*(?:г\.?)?",
    flags=re.IGNORECASE
)

RE_PROGRESSION = re.compile(
    r"Прогрессирование\s+от\s+(?P<date>\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{4}|\d{4})",
    flags=re.IGNORECASE
)

def date_to_iso_like(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s*г\.?$", "", s, flags=re.IGNORECASE)
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        return f"{yy}-{mm}-{dd}"
    m = re.fullmatch(r"(\d{2})\.(\d{4})", s)
    if m:
        mm, yy = m.group(1), m.group(2)
        return f"{yy}-{mm}"
    m = re.fullmatch(r"(19\d{2}|20\d{2})", s)
    if m:
        return m.group(1)
    return s

def parse_range(range_str: str) -> Tuple[Optional[str], Optional[str]]:
    m = RE_RANGE.search(range_str or "")
    if not m:
        return (None, None)
    return (date_to_iso_like(m.group("start")), date_to_iso_like(m.group("end")))

def extract_progression_dates(text: str) -> List[str]:
    out: List[str] = []
    for m in RE_PROGRESSION.finditer(text or ""):
        out.append(date_to_iso_like(m.group("date")))
    seen = set()
    uniq: List[str] = []
    for d in out:
        if d in seen:
            continue
        seen.add(d)
        uniq.append(d)
    return uniq


# ============================================================
# 4) TNM
# ============================================================

TNM_RE = re.compile(
    r"\b"
    r"(?P<prefix>(?:y|c|p|r|a|m|u|yp|yc|yr)?)\s*"
    r"T\s*(?P<t>is|x|[0-4](?:[a-d])?)\s*"
    r"N\s*(?P<n>x|[0-3](?:[a-c])?(?:\s*\(sn\))?)\s*"
    r"M\s*(?P<m>x|0|1(?:[a-c])?)"
    r"\b",
    flags=re.IGNORECASE
)

def extract_tnm_from_text(text: str) -> Optional[Dict[str, str]]:
    if not text:
        return None
    tnorm = (
        text.replace("М", "M")
            .replace("Х", "X")
            .replace("х", "x")
            .replace("с", "c")
            .replace("С", "c")
    )
    m = TNM_RE.search(tnorm)
    if not m:
        return None
    t = m.group("t").upper()
    n = m.group("n").upper().replace(" ", "")
    mm = m.group("m").upper()
    return {"t": f"T{t}", "n": f"N{n}".replace("(SN)", "(sn)"), "m": f"M{mm}"}


# ============================================================
# 5) TREATMENT LINES (rules)
# ============================================================

RE_PARENS = re.compile(r"\((?P<txt>[^()]{2,240})\)")

RE_COURSE_PREFIX = r"(?:\b\d+\s*(?:курс|введение)\b\s*)?"
RE_KIND = r"(?P<kind>ПХТ|ХТТ|ХТ|ИТ|МХТ)"
RE_LINE_WORD = r"(?P<line>\d{1,2})\s*(?:[- ]*(?:я)\s*)?линии?\b"

RE_LINE_THERAPY = re.compile(
    rf"{RE_COURSE_PREFIX}\b{RE_KIND}\s*{RE_LINE_WORD}"
    rf"(?P<after>[^.\n]{{0,320}}?)"
    rf"(?P<range>(?:с|c)\s*(?:\d{{2}}\.\d{{2}}\.\d{{4}}|\d{{2}}\.\d{{4}}|\d{{4}}).{{0,120}}?(?:по|-|—)\s*(?:\d{{2}}\.\d{{2}}\.\d{{4}}|\d{{2}}\.\d{{4}}|\d{{4}}))",
    flags=re.IGNORECASE
)

RE_LINE_SINGLE = re.compile(
    rf"{RE_COURSE_PREFIX}\b{RE_KIND}\s*{RE_LINE_WORD}"
    rf"(?P<after>[^.\n]{{0,320}}?)"
    rf"(?:\bот\s*(?P<date>\d{{2}}\.\d{{2}}\.\d{{4}}|\d{{2}}\.\d{{4}}|\d{{4}})\s*(?:г\.? )?)?",
    flags=re.IGNORECASE
)

def _extract_regimen(after: str) -> Optional[str]:
    if not after:
        return None
    pm = RE_PARENS.search(after)
    if pm:
        val = " ".join(pm.group("txt").split())
        return val or None
    cut = re.split(r"(?:\bс\b|\bc\b|\bпо\b|\bот\b)\s*\d", after, maxsplit=1, flags=re.IGNORECASE)[0]
    cut = " ".join(cut.split()).strip(" -—:;,")
    cut = re.sub(r"\b(с|по|от)\b.*$", "", cut, flags=re.IGNORECASE).strip(" -—:;,")
    return cut or None

def _infer_reason_for_change(ctx: str) -> Optional[str]:
    if not ctx:
        return None
    c = ctx.lower()
    # most important first
    if "аллерг" in c and "карбоплат" in c:
        return "аллергическая реакция на карбоплатин"
    if ("инфиц" in c or "инфекц" in c) and ("порт" in c or "порт-систем" in c or "портсистем" in c):
        return "инфицирование порт-системы"
    if "прогресс" in c:
        return "прогрессирование"
    if "токсич" in c or "неперенос" in c:
        return "токсичность/непереносимость"
    if "отмен" in c:
        return "отмена терапии (причина не уточнена)"
    return None


def extract_therapy_lines(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    rows: List[Dict[str, Any]] = []
    used_spans: List[Tuple[int, int]] = []

    def ctx_window(span: Tuple[int, int], left: int = 220, right: int = 260) -> str:
        a, b = span
        return t[max(0, a - left): min(len(t), b + right)]

    for m in RE_LINE_THERAPY.finditer(t):
        sp = m.span()
        used_spans.append(sp)
        kind = (m.group("kind") or "").upper()
        line = int(m.group("line"))
        after = m.group("after") or ""
        start, end = parse_range(m.group("range") or "")
        regimen = _extract_regimen(after)
        reason = _infer_reason_for_change(ctx_window(sp))
        rows.append({
            "line": line,
            "kind": kind,
            "regimen_name": regimen,
            "start_date": start,
            "end_date": end,
            "reason_for_change": reason,
            "span": sp,
        })

    def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
        return not (a[1] <= b[0] or b[1] <= a[0])

    for m in RE_LINE_SINGLE.finditer(t):
        sp = m.span()
        if any(_overlaps(sp, usp) for usp in used_spans):
            continue
        kind = (m.group("kind") or "").upper()
        line = int(m.group("line"))
        after = m.group("after") or ""
        d = m.group("date")
        start = date_to_iso_like(d) if d else None
        regimen = _extract_regimen(after)
        if not regimen and not start:
            continue
        reason = _infer_reason_for_change(ctx_window(sp))
        rows.append({
            "line": line,
            "kind": kind,
            "regimen_name": regimen,
            "start_date": start,
            "end_date": None,
            "reason_for_change": reason,
            "span": sp,
        })

    rows.sort(key=lambda r: (r["line"], r["start_date"] or ""))
    return rows


# ============================================================
# 6) DIAGNOSIS minimal
# ============================================================

RE_DISEASE_BREAST = re.compile(r"\bрак\b[^.\n]{0,60}\bмолочн\w+\s+желез\w+\b", flags=re.IGNORECASE)
RE_TNBC = re.compile(r"(трижды\s+негативн\w+|тройн\w+\s+негативн\w+)", flags=re.IGNORECASE)
RE_STAGE = re.compile(r"\b([IVX]{1,4}[ABC]?)\s*ст\.?", flags=re.IGNORECASE)

def extract_diagnosis_fields(text: str) -> Dict[str, Optional[str]]:
    t = text or ""
    disease = "Рак молочной железы" if RE_DISEASE_BREAST.search(t) else None
    subtype = None
    m = RE_TNBC.search(t)
    if m:
        subtype = "Трижды негативный подтип" if "трижды" in m.group(1).lower() else "Тройной негативный подтип"
    stage = None
    m = RE_STAGE.search(t)
    if m:
        stage = m.group(1).upper()
    return {"disease": disease, "subtype": subtype, "stage": stage}


# ============================================================
# 7) BIOMARKERS (полный блок)
# ============================================================

# IHC blocks with date: "ИГХ №2 от 07.09.2021: ...", "Гистология и ИГХ ... от 07.09.2021: ..."
RE_IHC_BLOCK = re.compile(
    r"(?P<head>(?:Гистология\s*и\s*ИГХ|ГИ\s*и\s*ИГХ|ИГХ)[^\n]{0,140}?)"
    r"(?:\s*№\s*\S+)?\s*"
    r"(?:от\s*(?P<d>\d{2}\.\d{2}\.\d{4})|в\s+(?P<y>(19|20)\d{2})\s*г)\s*[:\-]?\s*"
    r"(?P<body>.{0,1200})",
    flags=re.IGNORECASE | re.DOTALL
)

# IHC blocks without date: "ГИ и ИГХ: ... HER2-1+, Ki67 - 80%."
RE_IHC_BLOCK_NODATE = re.compile(
    r"(?P<head>(?:Гистология\s*и\s*ИГХ|ГИ\s*и\s*ИГХ|ИГХ)[^\n]{0,180}?)\s*[:\-]\s*"
    r"(?P<body>.{0,600})",
    flags=re.IGNORECASE | re.DOTALL
)

# Molecular genetics: "МГИ от 05.03.2022: ...", "МГИ ... от 01.03.2024: ..."
RE_MGI_BLOCK = re.compile(
    r"\bМГИ\b[^.\n]{0,160}?\bот\s*(?P<d>\d{2}\.\d{2}\.\d{4})\s*[:\-]?\s*(?P<body>.{0,1200})",
    flags=re.IGNORECASE | re.DOTALL
)

# Inside-block patterns (IHC)
RE_ER_IN = re.compile(r"\bER\b[^\d]{0,15}(?P<val>\d{1,3})\s*(?:балл\w*|б)?", flags=re.IGNORECASE)
RE_PR_IN = re.compile(r"\bPR\b[^\d]{0,15}(?P<val>\d{1,3})\s*(?:балл\w*|б)?", flags=re.IGNORECASE)
RE_HER2_IN = re.compile(
    r"\bHER[-\s]?2(?:\s*/?\s*neu|neu)?\b[^0-9]{0,25}(?P<val>0|1\+|2\+|3\+)",
    flags=re.IGNORECASE
)
RE_KI67_IN = re.compile(r"\bKi[-\s]?67\b[^\d]{0,15}(?P<val>\d{1,3})\s*%", flags=re.IGNORECASE)

# PD-L1 CPS separate (avoid duplicates)
RE_PDL1_BLOCK = re.compile(
    r"(?is)"
    r"(?:\bPD[-\s]?L1\b[^.\n]{0,140}?\bCPS\b[^0-9]{0,20}(?P<cps>\d{1,3})[^.\n]{0,120}?\bот\s*(?P<d>\d{2}\.\d{2}\.\d{4}))"
    r"|"
    r"(?:\bот\s*(?P<d2>\d{2}\.\d{2}\.\d{4})[^.\n]{0,160}?\bPD[-\s]?L1\b[^.\n]{0,140}?\bCPS\b[^0-9]{0,20}(?P<cps2>\d{1,3}))"
)

# Inside-block patterns (MGI)
RE_TMB_IN = re.compile(r"\bTMB\b[^\d]{0,20}(?P<val>\d+(?:[.,]\d+)?)", flags=re.IGNORECASE)
RE_MSS_IN = re.compile(r"\bMSS\b", flags=re.IGNORECASE)
RE_PMMR_IN = re.compile(r"\bpMMR\b", flags=re.IGNORECASE)
RE_DMMR_IN = re.compile(r"\bdMMR\b", flags=re.IGNORECASE)
RE_MSI_IN = re.compile(r"\bMSI(?:-H|-L)?\b", flags=re.IGNORECASE)
RE_BRCA_NEG_IN = re.compile(r"\bBRCA\s*1\s*/\s*2\b.*?\bне\s+обнаружен\w*", flags=re.IGNORECASE)

# Generic marker catcher (used inside IHC/MGI bodies)
RE_GENERIC_MARKER = re.compile(
    r"\b(?P<name>[A-Z][A-Z0-9\-\/]{1,12})\b"
    r"(?:\s*\((?P<note>[^()\n]{1,40})\))?"
    r"\s*[:=\-]?\s*"
    r"(?P<value>"
    r"(?:[0-9]{1,3}\s*%?)|"
    r"(?:[0-3]\+)|"
    r"(?:CPS\s*=?\s*\d{1,3})|"
    r"(?:TPS\s*=?\s*\d{1,3}\s*%?)|"
    r"(?:pos|neg|positive|negative|амплифицир\w*|мутац\w*|реаранжировк\w*|перестройк\w*|делец\w*|инсерц\w*|дик\w*\s*тип|wild\s*type|wt)|"
    r"(?:не\s+обнаружен\w*|обнаружен\w*)"
    r")",
    flags=re.IGNORECASE
)

KNOWN_MARKERS = {
    "ER", "PR", "HER2", "ERBB2", "KI67", "KI-67",
    "PD-L1", "PDL1", "TMB", "MSI", "MSS", "PMMR", "DMMR",
    "BRCA", "BRCA1", "BRCA2",
    "EGFR", "ALK", "ROS1", "BRAF", "KRAS", "NRAS", "MET", "RET", "NTRK",
    "PIK3CA",
}

def _std_marker_name(name_raw: str) -> str:
    n = (name_raw or "").strip()
    n_up = n.upper()

    if n_up in ("KI67", "KI-67"):
        return "ki67"
    if n_up in ("PDL1", "PD-L1"):
        return "pd-l1"
    if n_up in ("HER2", "HER-2", "ERBB2"):
        return "her2"
    if n_up == "ER":
        return "er"
    if n_up == "PR":
        return "pr"
    if n_up == "TMB":
        return "tmb"
    if n_up == "MSS":
        return "mss"
    if n_up == "MSI":
        return "msi"
    if n_up == "PMMR":
        return "pmmr"
    if n_up == "DMMR":
        return "dmmr"
    if n_up.startswith("BRCA"):
        return "brca"
    return n.lower()

def _clean_marker_value(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    v = " ".join(str(val).strip().split())
    v = v.replace(" ,", ",").replace(", ", ",")
    v = re.sub(r"(\d)\s*%\b", r"\1%", v)
    v = re.sub(r"(\d)\s*\+\b", r"\1+", v)
    return v or None

# --- context date helpers ---
RE_ANY_DATE = re.compile(
    r"(?P<d>\b\d{2}\.\d{2}\.\d{4}\b)|(?P<my>\b\d{2}\.\d{4}\b)|(?P<y>\b(19|20)\d{2}\b)"
)

def infer_context_date(text: str, a: int, b: int, window: int = 700) -> Optional[str]:
    """
    Ищет ближайшую дату вокруг фрагмента [a:b] в окне ±window.
    Приоритет: dd.mm.yyyy > mm.yyyy > yyyy. Возвращает в формате date_to_iso_like.
    """
    if not text:
        return None

    lo = max(0, a - window)
    hi = min(len(text), b + window)
    chunk = text[lo:hi]

    center = (a + b) // 2
    candidates = []

    for m in RE_ANY_DATE.finditer(chunk):
        raw = m.group(0)
        pos = lo + m.start()  # позиция в исходном тексте
        # дистанция до центра блока
        dist = abs(pos - center)

        kind = 3
        if m.group("d"):
            kind = 0
        elif m.group("my"):
            kind = 1
        elif m.group("y"):
            kind = 2

        candidates.append((kind, dist, raw))

    if not candidates:
        return None

    # сначала "тип даты", потом близость
    candidates.sort(key=lambda x: (x[0], x[1]))
    best_raw = candidates[0][2]
    return date_to_iso_like(best_raw)

def extract_biomarkers_min(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []

    def add(name_raw: str, value: Optional[str], date: Optional[str], source: str) -> None:
        out.append({
            "name_raw": name_raw,
            "name_std": _std_marker_name(name_raw),
            "value": _clean_marker_value(value),
            "unit": None,
            "date": date,
            "method": None,
            "source": source,
        })

    # 1) IHC blocks WITH date
    for m in RE_IHC_BLOCK.finditer(t):
        date: Optional[str] = None
        if m.group("d"):
            date = date_to_iso_like(m.group("d"))
        elif m.group("y"):
            date = m.group("y")

        body = m.group("body") or ""

        er = RE_ER_IN.search(body)
        pr = RE_PR_IN.search(body)
        her2 = RE_HER2_IN.search(body)
        ki = RE_KI67_IN.search(body)

        if er:
            add("ER", er.group("val"), date, "правила (ИГХ/ГИ блок)")
        if pr:
            add("PR", pr.group("val"), date, "правила (ИГХ/ГИ блок)")
        if her2:
            add("HER2", her2.group("val"), date, "правила (ИГХ/ГИ блок)")
        if ki:
            add("Ki67", (ki.group("val") or "").strip() + "%", date, "правила (ИГХ/ГИ блок)")

        # generic markers inside IHC (skip PD-L1 to avoid duplicates)
        for gm in RE_GENERIC_MARKER.finditer(body):
            nm = (gm.group("name") or "").strip()
            if not nm:
                continue
            nm_up = nm.upper()
            if nm_up in ("PD-L1", "PDL1"):
                continue
            if nm_up not in KNOWN_MARKERS:
                continue
            add(nm, gm.group("value"), date, "правила (ИГХ/ГИ блок, generic)")

    # 1b) IHC blocks WITHOUT date
    for m in RE_IHC_BLOCK_NODATE.finditer(t):
        body = m.group("body") or ""
        head = m.group("head") or ""
        src = norm_spaces((head + " " + body)[:320])
        date = infer_context_date(t, m.start(), m.end())

        er = RE_ER_IN.search(body)
        pr = RE_PR_IN.search(body)
        her2 = RE_HER2_IN.search(body)
        ki = RE_KI67_IN.search(body)

        if er:
            add("ER", er.group("val"), date, "правила (ИГХ/ГИ блок без даты): " + src)
        if pr:
            add("PR", pr.group("val"), date, "правила (ИГХ/ГИ блок без даты): " + src)
        if her2:
            add("HER2", her2.group("val"), date, "правила (ИГХ/ГИ блок без даты): " + src)
        if ki:
            add("Ki67", (ki.group("val") or "").strip() + "%", date, "правила (ИГХ/ГИ блок без даты): " + src)

        for gm in RE_GENERIC_MARKER.finditer(body):
            nm = (gm.group("name") or "").strip()
            if not nm:
                continue
            nm_up = nm.upper()
            if nm_up in ("PD-L1", "PDL1"):
                continue
            if nm_up not in KNOWN_MARKERS:
                continue
            add(nm, gm.group("value"), date, "правила (ИГХ/ГИ блок без даты, generic): " + src)

    # 2) PD-L1 CPS separate
    for m in RE_PDL1_BLOCK.finditer(t):
        cps = m.group("cps") or m.group("cps2")
        d = m.group("d") or m.group("d2")
        if cps and d:
            add("PD-L1 CPS", cps, date_to_iso_like(d), "правила (PD-L1 блок)")

    # 3) MGI blocks (split-by-header to avoid date bleeding)
    def _split_mgi_blocks(txt: str) -> List[Tuple[str, str]]:
        blocks: List[Tuple[str, str]] = []
        if not txt:
            return blocks
        rx = re.compile(r"\bМГИ\b[^.\n]{0,160}?\bот\s*(?P<d>\d{2}\.\d{2}\.\d{4})\b", re.IGNORECASE)
        starts = [(m.start(), m.group("d")) for m in rx.finditer(txt)]
        for i, (pos, d) in enumerate(starts):
            end_pos = starts[i + 1][0] if i + 1 < len(starts) else len(txt)
            blocks.append((d, txt[pos:end_pos]))
        return blocks

    for d_raw, block in _split_mgi_blocks(t):
        date = date_to_iso_like(d_raw)
        body = block or ""

        tmb = RE_TMB_IN.search(body)
        has_tmb = False
        if tmb:
            add("TMB", tmb.group("val").replace(",", "."), date, "правила (МГИ блок)")
            has_tmb = True

        if RE_MSS_IN.search(body):
            add("MSS", None, date, "правила (МГИ блок)")
        if RE_MSI_IN.search(body):
            add("MSI", RE_MSI_IN.search(body).group(0).upper(), date, "правила (МГИ блок)")
        if RE_PMMR_IN.search(body):
            add("pMMR", None, date, "правила (МГИ блок)")
        if RE_DMMR_IN.search(body):
            add("dMMR", None, date, "правила (МГИ блок)")
        if RE_BRCA_NEG_IN.search(body):
            add("BRCA1/2", "мутации не обнаружены", date, "правила (МГИ блок)")

        for gm in RE_GENERIC_MARKER.finditer(body):
            nm = (gm.group("name") or "").strip()
            if not nm:
                continue
            nm_up = nm.upper()
            if nm_up == "TMB" and has_tmb:
                continue
            if nm_up not in KNOWN_MARKERS:
                continue
            add(nm, gm.group("value"), date, "правила (МГИ блок, generic)")
# 4) de-dup by (name_std, value, date)
    uniq: List[Dict[str, Any]] = []
    seen = set()
    for b in out:
        key = (b["name_std"], (b["value"] or "").lower(), b["date"] or "")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)

    return uniq

# ============================================================
# 8) QUALITY GATE WARNINGS
# ============================================================

def add_quality_warnings(data: Dict[str, Any]) -> None:
    qg = data.get("quality_gate")
    if not isinstance(qg, dict):
        return
    issues = qg.setdefault("issues", [])
    if not isinstance(issues, list):
        qg["issues"] = []
        issues = qg["issues"]

    # biomarkers multi-values
    bms = data.get("biomarkers")
    if isinstance(bms, list):
        by_name: Dict[str, set] = {}
        for b in bms:
            if not isinstance(b, dict):
                continue
            n = (b.get("name_std") or b.get("name_raw") or "")
            n = str(n).lower().strip()
            v = b.get("value")
            v = str(v).strip() if v is not None else "null"
            by_name.setdefault(n, set()).add(v)

        for n, vals in by_name.items():
            if len(vals) >= 2 and n in ("her2", "ki67", "er", "pr", "pd-l1 cps", "pd-l1"):
                issues.append(
                    f"Найдено несколько значений для '{n}': {sorted(vals)}. "
                    f"Скорее всего это разные исследования по датам — проверьте поле biomarkers[].date."
                )

    # treatment overlaps
    th = data.get("treatment_history")
    if isinstance(th, list):
        intervals = []
        for row in th:
            if not isinstance(row, dict):
                continue
            s = row.get("start_date")
            e = row.get("end_date")
            if isinstance(s, str) and isinstance(e, str):
                intervals.append((s, e, row.get("line")))
        intervals.sort()
        for i in range(1, len(intervals)):
            prev_s, prev_e, prev_line = intervals[i - 1]
            cur_s, cur_e, cur_line = intervals[i]
            if cur_s == prev_e and (len(cur_s) == 7 or len(prev_e) == 7):
                continue
            if cur_s < prev_e:
                issues.append(
                    f"Перекрытие дат терапии: line {prev_line} ({prev_s}..{prev_e}) и line {cur_line} ({cur_s}..{cur_e}). "
                    f"Проверьте источники/регулярки."
                )
                break


# ============================================================
# 9) SMART FOCUS
# ============================================================

def select_relevant_text_smart(text: str, max_chars: int = 22000, *, window: int = 900) -> str:
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")

    focus_res = [
        # эти regex должны быть определены в BIOMARKERS BLOCK:
        RE_IHC_BLOCK,  # noqa: F821
        RE_MGI_BLOCK,  # noqa: F821
        RE_LINE_THERAPY,
        RE_LINE_SINGLE,
        RE_PROGRESSION,
        RE_RANGE,
        # доп. якоря для RT / процедур / метастазов / сопутствующих:
        RE_SRS_BRAIN,
        RE_PROC_REMOVE,
        RE_MTS_SITE,
        RE_INVASION_CHEST_WALL,
        RE_ALLERGY_CARBO,
        RE_DIVERT,
    ]

    spans: List[Tuple[int, int]] = []
    for rx in focus_res:
        for m in rx.finditer(t):
            a, b = m.span()
            if b <= a:
                continue
            spans.append((max(0, a - window), min(len(t), b + window)))

    if not spans:
        return select_relevant_text(t, max_chars=max_chars)

    spans.sort()
    merged: List[Tuple[int, int]] = []
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))

    parts = [t[s:e].strip() for s, e in merged if t[s:e].strip()]
    out = "\n\n---\n\n".join(parts)
    return select_relevant_text(out, max_chars=max_chars)


# ============================================================
# 10) LLM: missing therapy detection (ищейка пропусков)
# ============================================================

def normalize_regimen_key(s: str) -> str:
    """Очень грубая нормализация для дедупа."""
    if not s:
        return ""
    x = norm_spaces(s).lower()
    x = x.replace(" + ", "+").replace("+ ", "+").replace(" +", "+")
    # уберём окончания типа "эрибулином" -> "эрибулин" (очень примитивно)
    x = re.sub(r"\b(ом|ем|им|ой|ый|ая|ое|ые|ого|ему|ами|ами)\b", "", x)
    x = norm_spaces(x)
    return x


def build_missing_prompt(text: str, found_regimens: List[str]) -> str:
    found = [r for r in (found_regimens or []) if isinstance(r, str) and r.strip()]
    found = list(dict.fromkeys(found))[:80]  # ограничим
    payload = {
        "found_regimens": found
    }
    return f"""
ДАНО (уже найдено правилами):
{json.dumps(payload, ensure_ascii=False, indent=2)}

ТЕКСТ ИСТОРИИ БОЛЕЗНИ:
{text}

Верни ТОЛЬКО JSON вида: {{"found":[...]}}.
""".strip()


def ollama_find_missing_therapy(*, text: str, found_regimens: List[str], model: str) -> Dict[str, Any]:
    """
    LLM-ревизор терапии.

    Возвращает:
      - found: ВСЕ найденные LLM упоминания терапии (с quote⊂text),
      - missing: подмножество found, отсутствующее в found_regimens (по normalize_regimen_key),
      - present: подмножество found, уже присутствующее в found_regimens,
      - dropped: элементы, которые LLM предложила, но они не прошли минимальную валидацию (например quote не найден).
    """
    if ollama is None:
        raise RuntimeError(
            "Модуль 'ollama' не установлен. Либо установите его, либо запускайте с use_llm_missing_therapy=False."
        )

    raw = ollama_extract(
        model=model,
        system=SYSTEM_MISSING_THERAPY,
        user_prompt=build_missing_prompt(text, found_regimens),
    )
    rawj = extract_first_json_object(raw)
    if not looks_like_json_object(rawj):
        return {"found": [], "missing": [], "present": [], "dropped": [], "error": "not_json"}

    try:
        doc = parse_json_strict(rawj)
    except Exception as e:
        return {"found": [], "missing": [], "present": [], "dropped": [], "error": f"json_parse_error: {e}"}

    # поддержка старого формата {"missing":[...]} (legacy)
    found_any = doc.get("found")
    if not isinstance(found_any, list):
        found_any = doc.get("missing")
    if not isinstance(found_any, list):
        return {"found": [], "missing": [], "present": [], "dropped": [], "error": "found_not_list"}

    def _quote_match_level(q: str, full: str) -> str:
        if q and q in full:
            return "exact"
        qn = norm_spaces(q).lower()
        fn = norm_spaces(full).lower()
        return "normalized" if qn and qn in fn else "no"

    allowed_kind = {"chemo", "immunotherapy", "targeted", "hormone", "trial", "other"}
    allowed_mtype = {"administered", "planned", "recommended", "unclear"}
    allowed_conf = {"high", "medium", "low"}

    out: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []

    for m in found_any:
        if not isinstance(m, dict):
            continue

        regimen = m.get("regimen")
        quote = m.get("quote")

        if not isinstance(regimen, str) or not regimen.strip():
            continue
        if not isinstance(quote, str) or not quote.strip():
            continue

        kind = m.get("kind") if isinstance(m.get("kind"), str) else "other"
        kind = kind.strip().lower()
        if kind not in allowed_kind:
            kind = "other"

        mention_type = m.get("mention_type") if isinstance(m.get("mention_type"), str) else "unclear"
        mention_type = mention_type.strip().lower()
        if mention_type not in allowed_mtype:
            mention_type = "unclear"

        confidence = m.get("confidence") if isinstance(m.get("confidence"), str) else "medium"
        confidence = confidence.strip().lower()
        if confidence not in allowed_conf:
            confidence = "medium"

        date_hint = m.get("date_hint") if isinstance(m.get("date_hint"), str) and m.get("date_hint").strip() else None
        line_hint = m.get("line_hint") if isinstance(m.get("line_hint"), str) and m.get("line_hint").strip() else None
        note = m.get("note") if isinstance(m.get("note"), str) and m.get("note").strip() else None

        regimen = norm_spaces(regimen)
        quote = quote.strip()

        qm = _quote_match_level(quote, text)
        if qm == "no":
            dropped.append(
                {
                    "regimen": regimen,
                    "kind": kind,
                    "mention_type": mention_type,
                    "confidence": confidence,
                    "quote": quote,
                    "reason": "quote_not_found_in_text",
                }
            )
            continue

        out.append(
            {
                "regimen": regimen,
                "kind": kind,
                "mention_type": mention_type,
                "confidence": confidence,
                "quote": quote,
                "quote_match": qm,
                "date_hint": date_hint,
                "line_hint": line_hint,
                "note": note,
            }
        )

    # дедуп по regimen_key+quote
    uniq: List[Dict[str, Any]] = []
    seen = set()
    for x in out:
        key = (normalize_regimen_key(x["regimen"]), norm_spaces(x["quote"]).lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(x)

    existing_keys = set()
    for r in (found_regimens or []):
        if isinstance(r, str) and r.strip():
            existing_keys.add(normalize_regimen_key(r))

    present: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for x in uniq:
        rk = normalize_regimen_key(x["regimen"])
        if rk and rk in existing_keys:
            present.append(x)
        else:
            missing.append(x)

    return {
        "found": uniq,
        "missing": missing,
        "present": present,
        "dropped": dropped,
        "stats": {
            "found_total": len(uniq),
            "missing_total": len(missing),
            "present_total": len(present),
            "dropped_total": len(dropped),
        },
    }


def merge_missing_into_treatment_history(
    data: Dict[str, Any],
    missing_doc: Dict[str, Any],
    *,
    full_text: str,
) -> Dict[str, Any]:
    """
    Добавляет в treatment_history ТОЛЬКО те элементы, которые:
    - имеют quote, которая реально встречается в full_text (подстрока),
    - regimen не найден в уже существующих regimen_name (по нормализованному ключу),
    - без дат и без line.
    """
    if not isinstance(data, dict):
        return data

    th = data.get("treatment_history")
    if not isinstance(th, list):
        th = []
        data["treatment_history"] = th

    existing_keys = set()
    for r in th:
        if not isinstance(r, dict):
            continue
        rn = r.get("regimen_name")
        if isinstance(rn, str) and rn.strip():
            existing_keys.add(normalize_regimen_key(rn))

    missing = (missing_doc or {}).get("missing") or []
    if not isinstance(missing, list):
        return data

    added = 0
    for m in missing:
        if not isinstance(m, dict):
            continue
        regimen = m.get("regimen")
        quote = m.get("quote")
        if not (isinstance(regimen, str) and regimen.strip() and isinstance(quote, str) and quote.strip()):
            continue

        # 1) quote must be substring of text (strict gate)
        if quote not in full_text:
            # попробуем более мягко: нормализованные пробелы
            if norm_spaces(quote) not in norm_spaces(full_text):
                continue

        # 2) regimen dedup vs existing
        key = normalize_regimen_key(regimen)
        if not key or key in existing_keys:
            continue

        th.append(
            {
                "line": None,
                "regimen_name": regimen.strip(),
                "start_date": None,
                "end_date": None,
                "response": None,
                "reason_for_change": None,
                "drugs": [],
            }
        )
        existing_keys.add(key)
        added += 1

    if added:
        qg = data.get("quality_gate")
        if isinstance(qg, dict):
            issues = qg.setdefault("issues", [])
            if isinstance(issues, list):
                issues.append(f"treatment_history дополнен LLM-ищейкой пропусков: добавлено {added} эпизод(а/ов) без дат и без линий (строгая проверка quote⊂text).")

    return data


# ============================================================
# 11) Ensure minItems lists
# ============================================================

def ensure_minitems_lists(data: Dict[str, Any], empty: Dict[str, Any]) -> None:
    th = data.get("treatment_history")
    # anti-hallucination: не подставляем заглушки, если данных нет
    if not isinstance(th, list):
        data["treatment_history"] = []



# ============================================================
# 12) Build final case from rules
# ============================================================


# ============================================================
# 12) EXTRACTIONS: RT / PROCEDURES / METASTASES / ALLERGIES / COMORBIDITIES
# ============================================================

RE_SRS_BRAIN = re.compile(
    r"(?:стереотакс\w+|srs|sb\-?rt).{0,80}?(?:на|в)\s*(?:головн\w+\s+мозг|гм).{0,120}?(?P<d1>\d{2}\.\d{2}\.\d{4})\s*[–\-—]\s*(?P<d2>\d{2}\.\d{2}\.\d{4})",
    flags=re.IGNORECASE
)

RE_PROC_REMOVE = re.compile(
    r"(?:удален\w+|удаление)\s+(?P<what>имплант\w*|порт\-?систем\w*).{0,40}?(?P<d>\d{2}\.\d{2}\.\d{4})",
    flags=re.IGNORECASE
)

RE_ALLERGY_CARBO = re.compile(
    r"аллергическ\w+\s+реакц\w+[^.\n]{0,200}?карбоплатин",
    flags=re.IGNORECASE
)

RE_DIVERT = re.compile(
    r"дивертикул\w+[^.\n]{0,120}?(12\-?\s*перстн\w+\s+кишк\w+|двенадцатиперстн\w+\s+кишк\w+|сигмовидн\w+\s+кишк\w+)",
    flags=re.IGNORECASE
)

RE_MTS_SITE = re.compile(
    r"(?:\bмтс\b|метастаз\w+)[^.\n]{0,120}?(?P<site>головн\w+\s+мозг|гм|селез[её]нк\w+|грудн\w+\s+стенк\w+)[^.\n]{0,80}?(?P<d>\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{4}|\d{4})?",
    flags=re.IGNORECASE
)

RE_INVASION_CHEST_WALL = re.compile(
    r"инваз\w+[^.\n]{0,200}?(?:мышц\w+\s+грудн\w+\s+стенк\w+|мышц\w+[^.\n]{0,40}?грудн\w+\s+стенк\w+)",
    flags=re.IGNORECASE
)


def extract_radiotherapy_min(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []
    for m in RE_SRS_BRAIN.finditer(t):
        d1 = date_to_iso_like(m.group("d1"))
        d2 = date_to_iso_like(m.group("d2"))
        out.append({
            "site": "головной мозг",
            "start_date": d1,
            "end_date": d2,
            "technique": "стереотаксическая лучевая терапия",
            "dose": None,
            "fractions": None,
            "source": "ИБ (правила: стереотакс на ГМ)",
        })
    return out


def extract_procedures_min(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []
    for m in RE_PROC_REMOVE.finditer(t):
        what = (m.group("what") or "").lower()
        d = date_to_iso_like(m.group("d"))
        if "порт" in what:
            name = "удаление порт-системы"
        elif "имплант" in what:
            name = "удаление импланта"
        else:
            name = "процедура (удаление устройства)"
        out.append({"date": d, "name": name, "note": None, "source": "ИБ (правила: процедуры)"})
    return out


def extract_allergies_min(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []
    if RE_ALLERGY_CARBO.search(t):
        out.append({
            "substance": "карбоплатин",
            "reaction": "аллергическая реакция",
            "severity": None,
            "source": "ИБ (правила: аллергия)",
        })
    return out


def extract_comorbidities_min(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []
    for m in RE_DIVERT.finditer(t):
        # Строго буквальное значение из текста (без домыслов "дивертикулит" и без severity).
        name = (m.group(0) or "").strip()
        if name:
            out.append({
                "name": name,
                "severity": None,
                "status": "unknown",
                "source": "КТ/ИБ (правила: сопутствующие находки)",
            })
    # de-dup by name
    uniq = []
    seen = set()
    for x in out:
        k = x.get("name")
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq


def extract_metastases_min(text: str) -> List[Dict[str, Any]]:
    t = text or ""
    out: List[Dict[str, Any]] = []
    for m in RE_MTS_SITE.finditer(t):
        site_raw = (m.group("site") or "").lower()
        if site_raw in ("гм",):
            site = "головной мозг"
        elif "мозг" in site_raw:
            site = "головной мозг"
        elif "селез" in site_raw or "селезён" in site_raw or "селезен" in site_raw:
            site = "селезёнка"
        elif "груд" in site_raw:
            site = "грудная стенка"
        else:
            site = site_raw
        d = m.group("d")
        date = date_to_iso_like(d) if d and "." in d else (d or None)
        out.append({"site": site, "date": date, "details": None, "source": "ИБ (правила: метастазы)"})

    if RE_INVASION_CHEST_WALL.search(t):
        out.append({"site": "грудная стенка", "date": None, "details": "инвазия в мышцы грудной стенки", "source": "ИБ (правила: инвазия)"})

    # de-dup by (site, date, details)
    uniq = []
    seen = set()
    for x in out:
        k = (x.get("site"), x.get("date") or "", x.get("details") or "")
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq

def build_case_from_rules_min(*, text: str, empty: Dict[str, Any], case_id: str) -> Dict[str, Any]:
    data = json.loads(json.dumps(empty, ensure_ascii=False))

    if isinstance(data.get("meta"), dict):
        data["meta"]["case_id"] = case_id
        data["meta"]["language"] = "ru"
    # demographics (dob + sex)
    if isinstance(data.get("patient"), dict):
        demo = data["patient"].get("demographics")
        if isinstance(demo, dict):
            demo["dob"] = extract_dob(text)
            demo["sex"] = infer_sex(text)
            demo.pop("age_years", None)  # чтобы поле больше не всплывало
    diag_fields = extract_diagnosis_fields(text)
    if isinstance(data.get("diagnoses"), list) and data["diagnoses"] and isinstance(data["diagnoses"][0], dict):
        d0 = data["diagnoses"][0]
        if diag_fields["disease"]:
            d0["disease"] = diag_fields["disease"]
        if diag_fields["subtype"]:
            d0["subtype"] = diag_fields["subtype"]
        if diag_fields["stage"]:
            d0["stage"] = diag_fields["stage"]

    tnm = extract_tnm_from_text(text)
    if tnm and isinstance(data.get("diagnoses"), list) and data["diagnoses"]:
        if isinstance(data["diagnoses"][0], dict):
            data["diagnoses"][0]["tnm"] = tnm

    prog_dates = extract_progression_dates(text)
    if prog_dates and isinstance(data.get("diagnoses"), list) and data["diagnoses"]:
        d0 = data["diagnoses"][0]
        if isinstance(d0, dict) and isinstance(d0.get("dates"), dict):
            d0["dates"]["progression_dates"] = prog_dates

    # treatment_history by rules
    th: List[Dict[str, Any]] = []
    lines = extract_therapy_lines(text)
    for r in lines:
        th.append(
            {
                "line": r["line"],
                "regimen_name": r["regimen_name"],
                "start_date": r["start_date"],
                "end_date": r["end_date"],
                "response": None,
                "reason_for_change": r.get("reason_for_change"),
                "drugs": [],
            }
        )
    if th:
        data["treatment_history"] = th

    # biomarkers by rules (строго из safe-zones ИГХ/МГИ, без загрязнения интро)
    bms_obj = extract_biomarkers(text)  # type: ignore
    if bms_obj:
        data["biomarkers"] = [
            {
                "name_raw": b.name_raw,
                "name_std": b.name_std,
                "value": b.value,
                "unit": None,
                "date": b.date,
                "method": None,
                "source": b.source,
            }
            for b in bms_obj
        ]


    
    # allergies / comorbidities (minimum rules)
    if isinstance(data.get("patient"), dict):
        p0 = data["patient"]
        if isinstance(p0.get("allergies"), list):
            for a in extract_allergies_min(text):
                if not isinstance(a, dict):
                    continue
                key = (a.get("substance") or "", a.get("reaction") or "")
                if any((x.get("substance") or "", x.get("reaction") or "") == key for x in p0["allergies"] if isinstance(x, dict)):
                    continue
                p0["allergies"].append(a)

        if isinstance(p0.get("comorbidities"), list):
            for c in extract_comorbidities_min(text):
                if not isinstance(c, dict):
                    continue
                name = c.get("name") or ""
                if any((x.get("name") or "") == name for x in p0["comorbidities"] if isinstance(x, dict)):
                    continue
                p0["comorbidities"].append(c)

    # radiotherapy / procedures / metastases (если секции есть в шаблоне)
    if isinstance(data.get("radiotherapy"), list):
        data["radiotherapy"] = extract_radiotherapy_min(text)

    if isinstance(data.get("procedures"), list):
        data["procedures"] = extract_procedures_min(text)

    if isinstance(data.get("metastases"), list):
        data["metastases"] = extract_metastases_min(text)
    # issues baseline
    if isinstance(data.get("quality_gate"), dict):
        issues = data["quality_gate"].setdefault("issues", [])
        if not isinstance(issues, list):
            data["quality_gate"]["issues"] = []
            issues = data["quality_gate"]["issues"]
        bms = data.get("biomarkers") if isinstance(data.get("biomarkers"), list) else []
        if not tnm:
            issues.append("TNM не найден правилами")
        if not th:
            issues.append("Линии терапии не найдены правилами")
        if not bms:
            issues.append("Биомаркеры не найдены правилами")

    add_quality_warnings(data)
    return data


# ============================================================
# 13) MAIN
# ============================================================

def extract_case_json(
    *,
    input_path: str,
    case_id: str = "case_0001",
    model: str = "qwen2.5:7b-instruct",
    out_root: str = "data/outputs",
    clinical_normalize: bool = True,
    use_llm_missing_therapy: bool = True,
) -> Path:
    out_dir = Path(out_root) / case_id
    out_dir.mkdir(parents=True, exist_ok=True)

    extracted = extract_text(input_path, clinical=clinical_normalize)
    raw_text = extracted if isinstance(extracted, str) else extracted.text

    raw_text = apply_replacements(raw_text)
    (out_dir / "extracted.txt").write_text(raw_text, encoding="utf-8")

    # --- COVERAGE LAYER (FULL TEXT) ---
    coverage = build_coverage_layer(
        raw_text=raw_text,
        clean_text=raw_text,
        cleaner_version="v1.1",
        lang="ru",
        source_type=extracted.file_type if hasattr(extracted, "file_type") else "text",
    )
    (out_dir / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    rep = quality_check_coverage(coverage)
    (out_dir / "coverage_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    empty = load_json(TEMPLATE_PATH)
    schema = load_json(SCHEMA_PATH)

    timeline = ollama_extract_timeline(text=raw_text, model=model, out_dir=out_dir)
    (out_dir / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")

    # фокус для правил: склеиваем цитаты событий (это компактный “концентрат” дат/лечения)
    snips = []
    for e in (timeline.get("events") or []):
        if isinstance(e, dict):
            t = (e.get("text_snippet") or "").strip()
            if t:
                snips.append(t)

    focus_text = (raw_text[:2000] + "\n\n" + "\n\n".join(snips))[:22000]
    (out_dir / "focus.txt").write_text(focus_text, encoding="utf-8")
    # --- FOCUS: сначала пробуем сегменты, иначе fallback на старый smart ---

    # rules → base
    data = build_case_from_rules(
            text=focus_text,
            full_text=raw_text,     # <-- вот это ключевое
            template=empty,
            case_id=case_id,
        )
    # patient.* заполняем по ПОЛНОМУ тексту, а не по focus_text
    fill_patient_context_inplace(data, full_text=raw_text, broad=True)
    # LLM “ищейка пропусков”:
    if use_llm_missing_therapy:
        found_regimens = []
        th = data.get("treatment_history")
        if isinstance(th, list):
            for r in th:
                if isinstance(r, dict) and isinstance(r.get("regimen_name"), str) and r["regimen_name"].strip():
                    found_regimens.append(r["regimen_name"].strip())

        missing_doc = ollama_find_missing_therapy(text=raw_text, found_regimens=found_regimens, model=model)
        (out_dir / "llm_missing_therapy.json").write_text(
            json.dumps(missing_doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ВАЖНО: не смешиваем 'подозреваемые' упоминания терапии без дат/линий с treatment_history,
        # чтобы не загрязнять данные. Сохраняем только в llm_missing_therapy.json.
        if isinstance(data.get("quality_gate"), dict):
            issues = data["quality_gate"].setdefault("issues", [])
            if isinstance(issues, list) and (missing_doc.get("missing") or []):
                issues.append("Найдены возможные пропуски терапии (LLM-ищейка): см. llm_missing_therapy.json. Не добавлено в treatment_history из-за отсутствия дат/линий.")


    # --- Post-process: защита от логических инверсий и “каши” ---
    try:
        normalize_biomarkers_inplace(data, focus_text)
        enrich_treatment_history_inplace(data)
        normalize_progression_dates_inplace(data)
        add_progression_links_to_quality_gate(data)
    except Exception as _pp_err:
        # не валим пайплайн из-за постобработки, но фиксируем в quality_gate
        qg = data.setdefault("quality_gate", {}) if isinstance(data, dict) else {}
        if isinstance(qg, dict):
            issues = qg.setdefault("issues", [])
            if isinstance(issues, list):
                issues.append(f"Post-process warning: {_pp_err}")

    ensure_minitems_lists(data, empty)
    validate_or_raise(data, schema)

    out_path = out_dir / "case.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    input_path = "data/inbound/patient_00026.docx"
    out = extract_case_json(
        input_path=input_path,
        case_id="case_00026",
        clinical_normalize=True,
        use_llm_missing_therapy=True,
    )
    print(f"OK: saved {out}")