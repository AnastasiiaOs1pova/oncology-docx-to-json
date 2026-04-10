from __future__ import annotations

import copy
import re
import calendar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..normalize_med_text import apply_replacements
from .biomarkers import extract_biomarkers
from .dates import sort_key_date
from .demographics import fill_demographics_inplace
from .nosology import extract_nosology
from .progressions import extract_progressions
from .extra_findings import extract_metastases, extract_procedures, extract_radiotherapy
from .therapy import TherapyLine, extract_therapy_lines
from .tnm import extract_tnm

# Опционально: если у тебя есть агрегатор диагноза (ICD/stage/subtype/morphology)
try:
    from .diagnosis_rules import extract_primary_diagnosis  # type: ignore
except Exception:
    extract_primary_diagnosis = None  # type: ignore


# -------------------------
# Helpers: drug lexicon
# -------------------------

def _load_drug_phrases() -> List[str]:
    try:
        # .../src/rules/builder.py -> parents[2] = .../src
        src_dir = Path(__file__).resolve().parents[2]
        p = src_dir.parent / "resources" / "drugs.txt"
        if p.exists():
            phrases: List[str] = []
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = (line or "").strip().lower()
                if not s or s.startswith("#"):
                    continue
                phrases.append(s)
            phrases.sort(key=len, reverse=True)
            return phrases
    except Exception:
        pass

    return [
        "осимертиниб",
        "пеметрексед",
        "паклитаксел",
        "карбоплатин",
        "бевацизумаб",
        "атезолизумаб",
        "рамуцирумаб",
        "капецитабин",
        "трастузумаб",
        "иринотекан",
    ]


_DRUG_PHRASES = _load_drug_phrases()


def _find_first_drug_phrase(text: str) -> Optional[str]:
    low = (text or "").lower()
    if not low:
        return None
    for ph in _DRUG_PHRASES:
        if ph in low:
            return ph
    return None


# -------------------------
# Helpers: date overlap
# -------------------------

def _date_to_tuple(s: Optional[str], *, is_end: bool) -> Optional[Tuple[int, int, int]]:
    if not s:
        return None
    s = str(s).strip()

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if is_end:
            last = calendar.monthrange(y, mo)[1]
            return (y, mo, last)
        return (y, mo, 1)

    m = re.fullmatch(r"(\d{4})", s)
    if m:
        y = int(m.group(1))
        return (y, 12, 31) if is_end else (y, 1, 1)

    return None


def _intervals_overlap(a_start: Optional[str], a_end: Optional[str], b_start: Optional[str], b_end: Optional[str]) -> bool:
    a_s = _date_to_tuple(a_start, is_end=False)
    a_e = _date_to_tuple(a_end, is_end=True)
    b_s = _date_to_tuple(b_start, is_end=False)
    b_e = _date_to_tuple(b_end, is_end=True)
    if not a_s or not a_e or not b_s or not b_e:
        return False
    return max(a_s, b_s) <= min(a_e, b_e)


# -------------------------
# Helpers: therapy cleaning
# -------------------------

_RE_NOT_SYSTEMIC = re.compile(
    r"\b(лучев\w*\s*терап\w*|облуч\w*|стереотакс\w*\s*лт|радиотерап\w*|кибер\s*нож|гамма\s*нож)\b",
    flags=re.IGNORECASE,
)

_RE_DOSE_ONLY = re.compile(r"\b\d+(?:[\.,]\d+)?\s*(?:мг|mg|мкг|mcg|мл|ml|auc|грей|gy)\b", flags=re.IGNORECASE)


def _drug_set(text: Optional[str]) -> set[str]:
    low = (text or "").lower()
    out: set[str] = set()
    if not low:
        return out
    # берём только достаточно длинные фразы (чтобы не ловить шум)
    for ph in _DRUG_PHRASES:
        if len(ph) < 5:
            continue
        if ph in low:
            out.add(ph)
    return out


def _regimen_strength(reg: Optional[str], source: str) -> int:
    if not reg:
        # если режима нет, но в source есть препарат — это лучше, чем пусто
        return 2 if _find_first_drug_phrase(source) else 0
    r = reg.strip()
    low = r.lower()
    score = 0
    if "+" in r:
        score += 2
    if re.search(r"\b[A-Z]{2,}\b", r):
        score += 1
    if _find_first_drug_phrase(r) or _find_first_drug_phrase(source):
        score += 2
    if len(r) >= 6:
        score += 1
    if _RE_DOSE_ONLY.search(r) and not _find_first_drug_phrase(r):
        score -= 2
    if _RE_NOT_SYSTEMIC.search(low):
        score -= 3
    return score


def _norm_regimen_key(reg: Optional[str]) -> str:
    if not reg:
        return ""
    r = reg.lower().strip()
    r = re.sub(r"\s+", " ", r)
    r = r.strip(" .,:;—-\t")
    return r


def _clean_therapies(therapies: List[TherapyLine], *, full_text: str) -> Tuple[List[TherapyLine], List[str]]:
    """Чистим мусор/дубли и делаем мягкие эвристики для нумерации."""

    notes: List[str] = []

    # 1) доинференс режима, если он пустой, но в source есть препарат
    enriched: List[TherapyLine] = []
    removed = 0
    for tl in therapies:
        reg = tl.regimen
        if (reg is None or not str(reg).strip()) and tl.source:
            ph = _find_first_drug_phrase(tl.source)
            if ph:
                reg = ph

        # выкидываем совсем пустое
        if (reg is None or not str(reg).strip()) and not (tl.start or tl.end):
            removed += 1
            continue

        # выкидываем явно не системное лечение
        if (reg and _RE_NOT_SYSTEMIC.search(reg)) or _RE_NOT_SYSTEMIC.search(tl.source or ""):
            removed += 1
            continue

        # выкидываем дозировки без режима
        if reg and _RE_DOSE_ONLY.search(reg) and not _find_first_drug_phrase(reg):
            removed += 1
            continue

        enriched.append(
            TherapyLine(
                line=tl.line,
                kind=tl.kind,
                regimen=reg,
                start=tl.start,
                end=tl.end,
                source=tl.source,
            )
        )

    # 2) дедуп по (line, regimen, start, end)
    best_by_key: Dict[Tuple[Any, str, Any, Any], TherapyLine] = {}

    def score(tl: TherapyLine) -> Tuple[int, int]:
        # больше дат и сильнее режим -> лучше
        date_score = int(bool(tl.start)) + int(bool(tl.end))
        strength = _regimen_strength(tl.regimen, tl.source)
        return (date_score, strength)

    for tl in enriched:
        k = (tl.line, _norm_regimen_key(tl.regimen), tl.start, tl.end)
        cur = best_by_key.get(k)
        if cur is None or score(tl) > score(cur):
            best_by_key[k] = tl

    deduped = list(best_by_key.values())
    if removed:
        notes.append(f"therapy: удалено пустых/несистемных/дозовых фрагментов: {removed}")

    # 3) мягкая эвристика: если есть линии >=2, но нет 1 — присвоить 1 самой ранней сильной записи без номера
    explicit_lines = sorted({t.line for t in deduped if t.line is not None})
    if explicit_lines and (1 not in explicit_lines) and any(t.line is None for t in deduped):
        # берём самую раннюю запись без номера
        candidates = [t for t in deduped if t.line is None]
        candidates.sort(key=lambda x: sort_key_date(x.start))
        if candidates:
            cand = candidates[0]
            if _regimen_strength(cand.regimen, cand.source) >= 3:
                # присваиваем line=1
                new_list: List[TherapyLine] = []
                for t in deduped:
                    if t is cand:
                        new_list.append(
                            TherapyLine(
                                line=1,
                                kind=t.kind,
                                regimen=t.regimen,
                                start=t.start,
                                end=t.end,
                                source=t.source,
                            )
                        )
                    else:
                        new_list.append(t)
                deduped = new_list
                notes.append("therapy: line=1 присвоена эвристически (т.к. есть линии >=2, но нет 1)")

    # 4) если есть много номерных линий — жёстче чистим line=None мусор (как в case_0001)
    explicit_lines = sorted({t.line for t in deduped if t.line is not None})
    if explicit_lines:
        max_line = max(explicit_lines)
        if max_line >= 3:
            # найдём конец последней линии (если есть)
            last_end = None
            last_line = max_line
            for t in deduped:
                if t.line == last_line and t.end:
                    if last_end is None or sort_key_date(t.end) > sort_key_date(last_end):
                        last_end = t.end

            cleaned: List[TherapyLine] = []
            dropped_tail = 0
            for t in deduped:
                if t.line is None:
                    strength = _regimen_strength(t.regimen, t.source)
                    # если в кейсе много явных линий, эпизоды без номера без дат почти всегда мусор
                    if (t.start is None) or (t.end is None):
                        dropped_tail += 1
                        continue
                    # отбрасываем слабые эпизоды без номера после последней линии
                    if last_end and t.start and sort_key_date(t.start) >= sort_key_date(last_end) and strength < 4:
                        dropped_tail += 1
                        continue

                    # отбрасываем слабые эпизоды без номера вообще (если не несут препаратов)
                    if strength < 3:
                        dropped_tail += 1
                        continue

                    # отбрасываем, если это дубль по режиму и перекрывается с номерной линией
                    norm = _norm_regimen_key(t.regimen)
                    if norm:
                        for e in deduped:
                            if e.line is None:
                                continue
                            if _norm_regimen_key(e.regimen) == norm and _intervals_overlap(t.start, t.end, e.start, e.end):
                                dropped_tail += 1
                                break
                        else:
                            cleaned.append(t)
                    else:
                        dropped_tail += 1
                else:
                    cleaned.append(t)

            if dropped_tail:
                notes.append(f"therapy: удалено подозрительных эпизодов без номера (line=null): {dropped_tail}")
            deduped = cleaned

    # 4.1) удаляем line=None, которые являются подробными дублями номерных линий
    #      (дозировки/подробный протокол) и перекрываются по датам.
    explicit = [t for t in deduped if t.line is not None]
    unlined = [t for t in deduped if t.line is None]
    if explicit and unlined:
        explicit_sets = [(_drug_set(t.regimen or ""), t) for t in explicit]
        keep: List[TherapyLine] = []
        dropped = 0
        for u in deduped:
            if u.line is not None:
                keep.append(u)
                continue

            # drug-set берём по режиму, а если там аббревиатура (XELOX) — по source
            u_set = _drug_set(u.regimen) or _drug_set(u.source)
            if not u_set:
                # если это аббревиатура/протокол без расшифровки — не режем
                keep.append(u)
                continue

            drop_me = False
            for e_set, e in explicit_sets:
                if not e_set:
                    continue
                # если unlined является подмножеством/равенством drug-set номерной линии
                if u_set.issubset(e_set):
                    # если есть полные даты — используем overlap
                    if _intervals_overlap(u.start, u.end, e.start, e.end):
                        regtxt = (u.regimen or "")
                        if _RE_DOSE_ONLY.search(regtxt) or len(regtxt) > 80:
                            drop_me = True
                            break
                    # если дат нет/частично нет — сверяем хотя бы совпадение границы
                    if (u.start is None or u.end is None) and (
                        (u.end and u.end == e.end) or (u.start and u.start == e.start)
                    ):
                        drop_me = True
                        break

            if drop_me:
                dropped += 1
            else:
                keep.append(u)

        if dropped:
            notes.append(f"therapy: удалено дублей (line=null) по drug-set/датам: {dropped}")
        deduped = keep

    # 5) сортировка
    deduped.sort(key=lambda r: ((r.line if r.line is not None else 10_000), sort_key_date(r.start)))
    return deduped, notes


def _mark_overlaps(therapies: List[Dict[str, Any]]) -> List[str]:
    issues: List[str] = []
    lines = [t for t in therapies if t.get("line") is not None]
    # только если есть даты

    def tup(s: Optional[str], end: bool) -> Optional[Tuple[int, int, int]]:
        return _date_to_tuple(s, is_end=end)

    def nested(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        a_s, a_e = tup(a.get("start_date"), False), tup(a.get("end_date"), True)
        b_s, b_e = tup(b.get("start_date"), False), tup(b.get("end_date"), True)
        if not a_s or not a_e or not b_s or not b_e:
            return False
        return (b_s <= a_s <= a_e <= b_e) or (a_s <= b_s <= b_e <= a_e)
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a = lines[i]
            b = lines[j]
            # перекрытие нам нужно как подсветку конфликтов типа "line 8" vs "line 9".
            # При месячной точности соседние линии часто выглядят перекрывающимися.
            # Поэтому для соседних линий репортим только вложенность (nested), а не любой overlap.
            if abs(int(a.get("line")) - int(b.get("line"))) <= 1 and not nested(a, b):
                continue
            if _intervals_overlap(a.get("start_date"), a.get("end_date"), b.get("start_date"), b.get("end_date")):
                issues.append(
                    f"therapy: перекрытие по датам (пометка): line {a.get('line')} ({a.get('start_date')}..{a.get('end_date')}) и line {b.get('line')} ({b.get('start_date')}..{b.get('end_date')})"
                )
    return issues


# -------------------------
# Main builder
# -------------------------


def build_case_from_rules(
    *,
    text: str,
    template: Dict[str, Any],
    case_id: str = "case_0001",
    full_text: str | None = None,
) -> Dict[str, Any]:
    """Детерминированно заполняем case.json по шаблону на основе правил/регулярок."""

    # normalize once
    text = apply_replacements(text)
    full = apply_replacements(full_text) if full_text else text

    data = copy.deepcopy(template)

    # nosology
    nos, profile = extract_nosology(text)
    profile = profile or "unknown"

    # meta
    if isinstance(data.get("meta"), dict):
        data["meta"]["case_id"] = case_id
        data["meta"]["language"] = "ru"
        mvp = data["meta"].get("mvp_profile")
        if isinstance(mvp, dict):
            mvp["name"] = profile
            mvp["enabled"] = True

    # demographics
    fill_demographics_inplace(data, text=full)

    # diagnoses[0]
    if isinstance(data.get("diagnoses"), list) and data["diagnoses"] and isinstance(data["diagnoses"][0], dict):
        d0 = data["diagnoses"][0]

        if extract_primary_diagnosis is not None:
            diag, diag_profile = extract_primary_diagnosis(full)  # type: ignore
            if diag_profile and isinstance(data.get("meta"), dict) and isinstance(data["meta"].get("mvp_profile"), dict):
                data["meta"]["mvp_profile"]["name"] = diag_profile

            d0["disease"] = diag.get("disease") or nos or "Злокачественное новообразование (не уточнено)"
            for k in ("subtype", "icd10", "morphology", "stage"):
                if k in d0:
                    d0[k] = diag.get(k)
            if isinstance(d0.get("tnm"), dict) and isinstance(diag.get("tnm"), dict):
                d0["tnm"]["t"] = diag["tnm"].get("t")
                d0["tnm"]["n"] = diag["tnm"].get("n")
                d0["tnm"]["m"] = diag["tnm"].get("m")
        else:
            d0["disease"] = nos or "Злокачественное новообразование (не уточнено)"

    # TNM fallback (если diagnosis_rules не используется)
    tnm = extract_tnm(text)
    if tnm and isinstance(data.get("diagnoses"), list) and data["diagnoses"]:
        d0 = data["diagnoses"][0]
        if isinstance(d0, dict) and isinstance(d0.get("tnm"), dict):
            d0["tnm"]["t"] = tnm.get("t")
            d0["tnm"]["n"] = tnm.get("n")
            d0["tnm"]["m"] = tnm.get("m")

    # progressions
    progs = extract_progressions(text)
    prog_dates = [p.get("date") for p in progs if isinstance(p, dict) and p.get("date")]
    if prog_dates and isinstance(data.get("diagnoses"), list) and data["diagnoses"]:
        d0 = data["diagnoses"][0]
        if isinstance(d0, dict) and isinstance(d0.get("dates"), dict):
            d0["dates"]["progression_dates"] = prog_dates

    # therapies
    therapies_raw = extract_therapy_lines(full)
    therapies, therapy_notes = _clean_therapies(therapies_raw, full_text=full)

    th_rows: List[Dict[str, Any]] = []
    for tl in therapies:
        th_rows.append(
            {
                "line": int(tl.line) if tl.line is not None else None,
                "regimen_name": tl.regimen,
                "start_date": tl.start,
                "end_date": tl.end,
                "response": None,
                "reason_for_change": None,
                "drugs": [],
            }
        )

    # дедуп на всякий случай
    seen: set[Tuple[Any, Any, Any, Any]] = set()
    th_dedup: List[Dict[str, Any]] = []
    for r in th_rows:
        k = (r.get("line"), _norm_regimen_key(r.get("regimen_name")), r.get("start_date"), r.get("end_date"))
        if k in seen:
            continue
        seen.add(k)
        th_dedup.append(r)
    th_rows = th_dedup

    if isinstance(data.get("treatment_history"), list):
        data["treatment_history"] = []
    if th_rows:
        data["treatment_history"] = th_rows

    # biomarkers (по полному тексту)
    bms = extract_biomarkers(full)
    bm_rows: List[Dict[str, Any]] = []
    for b in bms:
        bm_rows.append(
            {
                "name_raw": b.name_raw,
                "name_std": b.name_std,
                "value": b.value,
                "unit": None,
                "date": b.date,
                "method": None,
                "source": "правила: " + (b.source[:220] if b.source else "история болезни"),
            }
        )

    if isinstance(data.get("biomarkers"), list):
        data["biomarkers"] = []
    if bm_rows:
        data["biomarkers"] = bm_rows

    
    # metastases / procedures / radiotherapy (по полному тексту; очень консервативно)
    if isinstance(data.get("metastases"), list):
        data["metastases"] = []
    mts = extract_metastases(full)
    if mts:
        data["metastases"] = mts

    if isinstance(data.get("procedures"), list):
        data["procedures"] = []
    procs = extract_procedures(full)
    if procs:
        data["procedures"] = procs

    if isinstance(data.get("radiotherapy"), list):
        data["radiotherapy"] = []
    rts = extract_radiotherapy(full)
    if rts:
        data["radiotherapy"] = rts
    issues: List[str] = []
    if tnm is None:
        issues.append("TNM не найден правилами")
    if not th_rows:
        issues.append("Терапия не найдена правилами")
    if not bm_rows:
        issues.append("Биомаркеры не найдены правилами")
    if not nos:
        issues.append("Нозология не нормализована (nosology_aliases.json не дал совпадений)")

    issues.extend(therapy_notes)
    issues.extend(_mark_overlaps(th_rows))

    if isinstance(data.get("quality_gate"), dict):
        data["quality_gate"].setdefault("issues", [])
        if isinstance(data["quality_gate"].get("issues"), list):
            data["quality_gate"]["issues"].extend(issues)
        else:
            data["quality_gate"]["issues"] = issues
    else:
        data["quality_gate"] = {"issues": issues}

    return data
