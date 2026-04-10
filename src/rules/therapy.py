from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .dates import DATE_ANY, date_to_iso_like, parse_range, sort_key_date
from .text_utils import norm_spaces, split_before_date_words, strip_trailing_punct

_RE_COURSE_PREFIX = r"(?:\b\d{1,2}\s*(?:курс|курса|курсов|цикл|цикла|циклов)\s*)?"

_RE_LINE_NO = r"(?P<line>\d+)\s*(?:[-–—]?\s*(?:я|й))?"

RE_LINE_THERAPY = re.compile(
    _RE_COURSE_PREFIX
    + r"\b(?P<kind>ПХТ|ХТТ|ХТ|ИТ|МХТ)\s*" + _RE_LINE_NO + r"\s*линии?\b"
    # after делаем ЖАДНЫМ (без '?'), иначе при опциональной дате regex матчится "обрезком"
    r"(?P<after>(?:[^.\n]|(?<=\d)\.(?=\d)){0,260})"
    rf"(?P<range>(?:с|c)\s*(?:{DATE_ANY}).{{0,80}}?(?:по|-|—)\s*(?:{DATE_ANY}))",
    flags=re.IGNORECASE,
)

RE_LINE_SINGLE = re.compile(
    _RE_COURSE_PREFIX
    + r"\b(?P<kind>ПХТ|ХТТ|ХТ|ИТ|МХТ)\s*" + _RE_LINE_NO + r"\s*линии?\b"
    r"(?P<after>(?:[^.\n]|(?<=\d)\.(?=\d)){0,260})",
    flags=re.IGNORECASE,
)

# Частая форма в учебных кейсах/выписках:
# "Лечение: 1 линия (07-12.2025): иринотекан+... (FOLFIRINOX)"
# Частая форма в учебных кейсах/выписках:
# "Лечение: 1 линия (07-12.2025): ..." или "Лечение и динамика: 1 линия (12.2023-04.2024): ..."
RE_LINE_TREATMENT_TEXT = re.compile(
    r"\b(?:лечение|терапия)\b(?:\s+и\s+динамик\w+)?\s*[:\-]?\s*"
    + _RE_LINE_NO
    + r"\s*лини(?:я|и)\b"
    r"(?P<after>(?:[^.\n]|(?<=\d)\.(?=\d)){0,420})",
    flags=re.IGNORECASE,
)

RE_LINE_BARE = re.compile(
    # "1 линия при ... (05-10.2025): FOLFIRI+панитумумаб"
    _RE_LINE_NO
    + r"\s*лини(?:я|и)\b"
    r"(?P<after>(?:[^.\n]|(?<=\d)\.(?=\d)){0,420})",
    flags=re.IGNORECASE,
)

RE_MONTHSPAN = re.compile(
    r"(?P<m1>\d{1,2})\s*[-–—]\s*(?P<m2>\d{1,2})\.(?P<y>\d{4})"
)

RE_PAREN_MONTHSPAN = re.compile(
    r"\(\s*(?P<m1>\d{1,2})\s*[-–—]\s*(?P<m2>\d{1,2})\.(?P<y>\d{4})\s*\)"
)

RE_PLAN_WORDS = re.compile(r"(?i)\b(рекоменд\w*|планир\w*|возможн\w*|рассмотр\w*|предлож\w*|показан\w*)\b")

RE_THERAPY_START = re.compile(rf"(?:\bот\b|\bс\b|\bc\b)\s*(?P<date>{DATE_ANY})", flags=re.IGNORECASE)
RE_THERAPY_END = re.compile(rf"(?:\bпо\b)\s*(?P<date>{DATE_ANY})", flags=re.IGNORECASE)
RE_THERAPY_UNTIL = re.compile(rf"(?:\bдо\b)\s*(?P<date>{DATE_ANY})", flags=re.IGNORECASE)

RE_PARENS = re.compile(r"\((?P<txt>[^()]{2,220})\)")

RE_PAREN_DATE = re.compile(
    rf"\(\s*(?P<date>{DATE_ANY})\s*\)",
    flags=re.IGNORECASE,
)

# общий диапазон дат: "с ... по ..." (или "c ... - ...")
RE_ANY_RANGE = re.compile(
    rf"(?:с|c)\s*(?P<start>{DATE_ANY})\s*(?:г\.?)?\s*(?:по|-|—)\s*(?P<end>{DATE_ANY})\s*(?:г\.?)?",
    flags=re.IGNORECASE,
)

# диапазон без "с/по": "01.2022-10.2024" / "08.24—03.25" и т.п.
RE_ANY_RANGE_BARE = re.compile(
    rf"(?P<start>{DATE_ANY})\s*(?:г\.?)?\s*(?:-|—|–)\s*(?P<end>{DATE_ANY})\s*(?:г\.?)?",
    flags=re.IGNORECASE,
)

# эвристика: контекст должен быть "про лечение", иначе это просто интервал наблюдения
RE_THERAPY_HINT = re.compile(
    r"\b(ПХТ|ХТТ|ХТ|МХТ|ИТ|таргет\w*|иммунотерап\w*|химиотерап\w*|протокол\w*|по\s+схем\w*|по\s+протокол\w*|режим\w*|монотерап\w*|поддерживающ\w*|курс\w*|введени\w*)\b",
    flags=re.IGNORECASE,
)

# явно НЕ системная терапия: чтобы не плодить мусор в line=None
RE_NOT_SYSTEMIC = re.compile(
    r"\b(лучев\w*\s*терап\w*|облуч\w*|стереотакс\w*\s*лт|радиотерап\w*|кибер\s*нож|гамма\s*нож)\b",
    flags=re.IGNORECASE,
)

RE_SINGLE_ADMIN = re.compile(
    r"\b(1\s*(?:введени\w*|инфузи\w*|курс\w*)|одно\s*(?:введени\w*|инфузи\w*|курс\w*))\b",
    flags=re.IGNORECASE,
)


def _load_drug_phrases() -> List[str]:
    """Загружаем словарь препаратов (МНН) для фильтрации мусора и нормальной эвристики режимов."""
    try:
        # .../src/rules/therapy.py -> parents[2] = .../src
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

    # fallback (минимум)
    return [
        "осимертиниб",
        "пеметрексед",
        "паклитаксел",
        "карбоплатин",
        "цисплатин",
        "капецитабин",
        "оксалиплатин",
        "рамуцирумаб",
        "бевацизумаб",
        "атезолизумаб",
        "трастузумаб дерукстекан",
        "трастузумаб",
        "иринотекан",
        "доксорубицин",
    ]


_DRUG_PHRASES = _load_drug_phrases()


def _has_drug_phrase(s: str) -> bool:
    low = (s or "").lower()
    if not low:
        return False
    for ph in _DRUG_PHRASES:
        if ph and ph in low:
            return True
    return False


def _is_bad_regimen(reg: str) -> bool:
    if not reg:
        return True
    low = reg.lower().strip()
    if RE_NOT_SYSTEMIC.search(low):
        return True
    # дозировки/единицы без препарата
    if re.fullmatch(r"[\d\s\.,+\-–—/:]*", low):
        return True
    if re.search(r"\b(?:мг|mg|мкг|mcg|мл|ml|auc|грей|gy)\b", low) and not _has_drug_phrase(low):
        return True
    return False

RE_REGIMEN_MARKED = re.compile(
    r"(?:по\s+(?:протокол\w*|схем\w*|режим\w*)\s*[:\-]?\s*|протокол\w*\s*[:\-]\s*|схем\w*\s*[:\-]\s*)"
    r"(?P<reg>[^\n\r.;]{2,140})",
    flags=re.IGNORECASE,
)

RE_VERB_REGIMEN = re.compile(
    r"\b(?:получал\w*|получает|проводил\w*|проводилась|проведено|назначен\w*|выполнено)\b\s*(?P<reg>[^\n\r.;]{2,140})",
    flags=re.IGNORECASE,
)

RE_TAIL_REG = re.compile(r"^\s*[,;:—\-]?\s*(?P<reg>[^\n\r.]{2,140})")


def _extract_regimen(after: str) -> Optional[str]:
    """
    Извлекаем режим/препарат(ы) из фрагмента строки.

    Правило безопасности: не брать содержимое скобок автоматически,
    т.к. там часто встречаются пояснения (например, "(на фоне MSI-H)").
    """
    if not after:
        return None

    src = norm_spaces(after)

    # 1) Явно размечено: "схема:/протокол:/режим:"
    m = RE_REGIMEN_MARKED.search(src)
    if m:
        reg = strip_trailing_punct(norm_spaces(m.group("reg")))
        reg = re.split(r"\b\d+\s*(?:циклов|курс\w*|введен\w*)\b", reg, 1, flags=re.IGNORECASE)[0].strip()
        reg = re.split(r"\b\d+\s*(?:мг|mg|мкг|mcg|мл|ml|auc)\b", reg, 1, flags=re.IGNORECASE)[0].strip()
        if reg and not _is_bad_regimen(reg):
            return reg[:140]

    # 2) Обработка скобок:
    #    - если внутри короткая аббревиатура режима (FOLFOX/AC/EC и т.п.) — можно использовать,
    #      и, при наличии, добавить препарат после ")+" (например, "FOLFOX+бевацизумаб").
    #    - если внутри пояснение ("на фоне ...", биомаркеры и т.п.) — выкидываем скобку и продолжаем.
    pure_abbr = None
    abbr_match = re.search(r"\(([A-Z0-9][A-Z0-9+\-]{1,15})\)", src)
    if abbr_match:
        pure_abbr = abbr_match.group(1)

    # убираем явно пояснительные скобки
    src_wo_notes = re.sub(
        r"\((?:на\s+фоне|в\s+связи|по\s+поводу|при|после|до|в\s+рамках)[^\)]{1,60}\)",
        " ",
        src,
        flags=re.IGNORECASE,
    )

    # если в скобках не аббревиатура (или есть пробелы) — часто это пояснение; уберём такие скобки, но оставим текст вокруг
    src_wo_notes = re.sub(r"\((?P<txt>[^\)]{1,60})\)",
                          lambda m: " " if (" " in m.group('txt') and "+" not in m.group('txt') and not _has_drug_phrase(m.group('txt'))) else m.group(0),
                          src_wo_notes)

    # 3) Основной кандидат — хвост до дат/слов-ограничителей
    cut = split_before_date_words(src_wo_notes)
    cut = strip_trailing_punct(norm_spaces(cut))
    cut = re.sub(r"\b(с|по|от|до)\b.*$", "", cut, flags=re.IGNORECASE).strip(" -—:;,")

    # отрезаем дозы/расписания/кол-во циклов
    cut = re.split(r"\b\d+\s*(?:циклов|курс\w*|введен\w*)\b", cut, 1, flags=re.IGNORECASE)[0].strip()
    cut = re.split(r"\b\d+\s*(?:мг|mg|мкг|mcg|мл|ml|auc)\b", cut, 1, flags=re.IGNORECASE)[0].strip()
    cut = re.split(r"\bкажд\w+\b", cut, 1, flags=re.IGNORECASE)[0].strip()

    # 4) Если есть аббревиатура и после неё явно идёт препарат через '+', соберём компактно
    if pure_abbr and pure_abbr in cut and "+" in cut:
        # пример: "... (FOLFOX)+бевацизумаб" -> "FOLFOX+бевацизумаб"
        m2 = re.search(r"\(" + re.escape(pure_abbr) + r"\)\s*\+\s*(?P<drug>[^,.;]{2,60})", src, flags=re.IGNORECASE)
        if m2:
            drug = strip_trailing_punct(norm_spaces(m2.group('drug')))
            drug = re.split(r"\b\d+\s*(?:мг|mg|мкг|mcg|мл|ml|auc)\b", drug, 1, flags=re.IGNORECASE)[0].strip()
            reg = f"{pure_abbr}+{drug}" if drug else pure_abbr
            if reg and not _is_bad_regimen(reg):
                return reg[:140]

    if cut and not _is_bad_regimen(cut):
        return cut[:140]
    return None

def _guess_kind(ctx: str) -> str:
    m = re.search(r"\b(ПХТ|ХТТ|ХТ|МХТ|ИТ)\b", ctx, flags=re.IGNORECASE)
    if m:
        return (m.group(1) or "THERAPY").upper()
    if re.search(r"\bтаргет\w*\b", ctx, flags=re.IGNORECASE):
        return "ТТ"
    if re.search(r"\bиммунотерап\w*\b", ctx, flags=re.IGNORECASE):
        return "ИТ"
    return "THERAPY"


def _extract_regimen_near_range(ctx: str, rel_start: int, rel_end: int) -> Optional[str]:
    """Достаём режим вокруг найденного диапазона дат."""
    before = ctx[:rel_start]
    after = ctx[rel_end:]

    # 1) явно размечено: "по протоколу: ..." / "схема: ..." — чаще в части AFTER
    m = RE_REGIMEN_MARKED.search(after)
    if m:
        reg = strip_trailing_punct(norm_spaces(m.group("reg")))
        if reg and not _is_bad_regimen(reg):
            return reg[:140]
        return None

    # 2) иногда то же самое написано перед диапазоном
    m = RE_REGIMEN_MARKED.search(before)
    if m:
        reg = strip_trailing_punct(norm_spaces(m.group("reg")))
        if reg and not _is_bad_regimen(reg):
            return reg[:140]
        return None

    # 3) "получал/назначен ... с ... по ..." — режим обычно в конце before
    tail = before[-180:]
    m = RE_VERB_REGIMEN.search(tail)
    if m:
        reg = strip_trailing_punct(norm_spaces(m.group("reg")))
        # обрезаем хвост перед датами, если протек
        reg = split_before_date_words(reg)
        reg = strip_trailing_punct(norm_spaces(reg))
        # защита от "состояние после..." и прочих нережимов
        if reg and (
            "+" in reg
            or _has_drug_phrase(reg)
            or re.search(r"\b[A-Z]{2,}\b", reg)
            or re.search(r"\b(?:протокол\w*|схем\w*|режим\w*)\b", reg, flags=re.IGNORECASE)
        ):
            if not _is_bad_regimen(reg):
                return reg[:140]
        return None

    # 4) "XELOX с ... по ..." — режим непосредственно перед "с"
    # берём последнюю "клауза" перед диапазоном
    clause = re.split(r"[\n\r.;]", tail)[-1]
    clause = strip_trailing_punct(norm_spaces(clause))
    clause = re.sub(r"\b(?:с|c)$", "", clause, flags=re.IGNORECASE).strip(" -—:;,")
    # эвристика: режим обычно содержит '+' или латиницу/цифры или слово протокол/схема
    if clause and (
        "+" in clause
        or re.search(r"\b[A-Z]{2,}\b", clause)
        or re.search(r"\b(?:протокол\w*|схем\w*)\b", clause, flags=re.IGNORECASE)
        or _has_drug_phrase(clause)
    ):
        # если в клаузе есть "протокол XELOX" — вытащим только правую часть
        clause = re.sub(r"^.*?\b(?:протокол\w*|схем\w*)\b\s*", "", clause, flags=re.IGNORECASE).strip(" :-—")
        if clause and not _is_bad_regimen(clause):
            return clause[:140]
        return None

    # 5) как fallback — попробуем вынуть режим из after (первые слова)
    head = strip_trailing_punct(norm_spaces(after[:160]))
    head = re.sub(r"^(?:г\.?\s*)", "", head, flags=re.IGNORECASE)
    if head and _has_drug_phrase(head) and not _is_bad_regimen(head):
        return head[:140]

    return None


@dataclass
class TherapyLine:
    line: Optional[int]
    kind: str
    regimen: Optional[str]
    start: Optional[str]
    end: Optional[str]
    source: str


def extract_therapy_lines(text: str) -> List[TherapyLine]:
    t = text or ""
    rows: List[TherapyLine] = []

    used_spans: List[Tuple[int, int]] = []
    for m in RE_LINE_THERAPY.finditer(t):
        used_spans.append(m.span())

        kind = (m.group("kind") or "").upper()
        line = int(m.group("line"))
        after = m.group("after") or ""
        start, end = parse_range(m.group("range") or "")
        regimen = _extract_regimen(after)
        if not regimen:
            tail = t[m.end() : min(len(t), m.end() + 200)]
            tm = RE_TAIL_REG.search(tail)
            if tm:
                cand = strip_trailing_punct(norm_spaces(tm.group("reg")))
                cand = re.sub(r"^\s*г\.?\s*[,;:—\-]?\s*", "", cand, flags=re.IGNORECASE)
                if cand and (
                    "+" in cand or RE_THERAPY_HINT.search(cand) or _has_drug_phrase(cand)
                ) and not _is_bad_regimen(cand):
                    regimen = cand[:140]

        if regimen and _is_bad_regimen(regimen):
            regimen = None

        # "1 введение" -> end_date = start_date (если start есть)
        if start and not end and RE_SINGLE_ADMIN.search(after):
            end = start

        span = t[max(0, m.start() - 80) : min(len(t), m.end() + 120)]
        if start and not end and RE_SINGLE_ADMIN.search(span):
            end = start
        rows.append(
            TherapyLine(
                line=line,
                kind=kind,
                regimen=regimen,
                start=start,
                end=end,
                source=norm_spaces(span),
            )
        )

    def overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
        return not (a[1] <= b[0] or b[1] <= a[0])

    for m in RE_LINE_SINGLE.finditer(t):
        sp = m.span()
        if any(overlaps(sp, usp) for usp in used_spans):
            continue

        kind = (m.group("kind") or "").upper()
        line = int(m.group("line"))
        after = m.group("after") or ""

        # Даты: сначала диапазон 'с ... по ...', затем одиночные 'от ...' или 'с ...'/'по ...'
        start, end = parse_range(after)
        if not start:
            dm = RE_THERAPY_START.search(after)
            if dm:
                start = date_to_iso_like(dm.group("date"))
        if not end:
            dm = RE_THERAPY_END.search(after)
            if dm:
                end = date_to_iso_like(dm.group("date"))
        if not end:
            dm = RE_THERAPY_UNTIL.search(after)
            if dm:
                end = date_to_iso_like(dm.group("date"))

        regimen = _extract_regimen(after)
        if not regimen:
            tail = t[m.end() : min(len(t), m.end() + 200)]
            tm = RE_TAIL_REG.search(tail)
            if tm:
                cand = strip_trailing_punct(norm_spaces(tm.group("reg")))
                cand = re.sub(r"^\s*г\.?\s*[,;:—\-]?\s*", "", cand, flags=re.IGNORECASE)
                if cand and (
                    "+" in cand or RE_THERAPY_HINT.search(cand) or _has_drug_phrase(cand)
                ) and not _is_bad_regimen(cand):
                    regimen = cand[:140]

        if regimen and _is_bad_regimen(regimen):
            regimen = None

        if start and not end and RE_SINGLE_ADMIN.search(after):
            end = start

        if not regimen and not start and not end:
            continue

        span = t[max(0, m.start() - 80) : min(len(t), m.end() + 120)]
        if start and not end and RE_SINGLE_ADMIN.search(span):
            end = start
        rows.append(
            TherapyLine(
                line=line,
                kind=kind,
                regimen=regimen,
                start=start,
                end=end,
                source=norm_spaces(span),
            )
        )

    # 2b) "Лечение: 1 линия (07-12.2025): ..." (без маркера ПХТ/ХТ)
    for m in RE_LINE_TREATMENT_TEXT.finditer(t):
        sp = m.span()
        if any(overlaps(sp, usp) for usp in used_spans):
            continue

        line = int(m.group("line"))
        after = m.group("after") or ""
        # не вытаскиваем планы/рекомендации
        if RE_PLAN_WORDS.search(after[:120]):
            continue

        # даты
        start, end = parse_range(after)
        if not start:
            ms = RE_MONTHSPAN.search(after)
            if ms:
                y = ms.group("y")
                m1 = int(ms.group("m1")); m2 = int(ms.group("m2"))
                if 1 <= m1 <= 12 and 1 <= m2 <= 12:
                    start = f"{y}-{m1:02d}"
                    end = f"{y}-{m2:02d}"

        if not start:
            mb = RE_ANY_RANGE_BARE.search(after)
            if mb:
                start = date_to_iso_like(mb.group("start"))
                end = date_to_iso_like(mb.group("end"))

        # режим: после ")" или после двоеточия
        reg_part = after
        reg_part = re.sub(r"^\s*\([^\)]{2,40}\)\s*[:\-—]?\s*", "", reg_part)
        reg_part = re.sub(r"^\s*[:\-—]\s*", "", reg_part)
        # часто сразу после даты стоит ":"
        if ":" in reg_part[:60]:
            reg_part = reg_part.split(":", 1)[1].strip()
        regimen = _extract_regimen(reg_part)
        if not regimen:
            # fallback: взять первые 140 символов и обрезать на запятой
            cand = strip_trailing_punct(norm_spaces(reg_part))
            cand = cand.split(",", 1)[0].strip()
            if cand and ("+" in cand or _has_drug_phrase(cand) or re.search(r"\b[A-Z]{2,}\b", cand)):
                if not _is_bad_regimen(cand):
                    regimen = cand[:140]

        if regimen and _is_bad_regimen(regimen):
            regimen = None

        if not regimen and not start and not end:
            continue

        span = t[max(0, m.start() - 80) : min(len(t), m.end() + 140)]
        rows.append(
            TherapyLine(
                line=line,
                kind="THERAPY",
                regimen=regimen,
                start=start,
                end=end,
                source=norm_spaces(span),
            )
        )
        used_spans.append(sp)

    # 2c) "1 линия при ... (05-10.2025): ..." (без слова 'лечение' и без маркера ПХТ/ХТ)
    for m in RE_LINE_BARE.finditer(t):
        sp = m.span()
        if any(overlaps(sp, usp) for usp in used_spans):
            continue

        line = int(m.group("line"))
        after = m.group("after") or ""

        # фильтр планов/рекомендаций (консервативно)
        if RE_PLAN_WORDS.search(after[:140]):
            continue

        # должны быть признаки терапии (иначе это может быть "1 линия обследования" и т.п.)
        head = after[:260]
        if not (RE_THERAPY_HINT.search(head) or _has_drug_phrase(head) or "+" in head or re.search(r"\b[A-Z]{2,}\b", head)):
            continue

        start, end = parse_range(after)
        if not start:
            ms = RE_MONTHSPAN.search(after)
            if ms:
                y = ms.group("y")
                m1 = int(ms.group("m1")); m2 = int(ms.group("m2"))
                if 1 <= m1 <= 12 and 1 <= m2 <= 12:
                    start = f"{y}-{m1:02d}"
                    end = f"{y}-{m2:02d}"
        if not start:
            mb = RE_ANY_RANGE_BARE.search(after)
            if mb:
                start = date_to_iso_like(mb.group("start"))
                end = date_to_iso_like(mb.group("end"))

        reg_part = after
        reg_part = re.sub(r"^\s*\([^\)]{2,60}\)\s*[:\-—]?\s*", "", reg_part)
        reg_part = re.sub(r"^\s*[:\-—]\s*", "", reg_part)
        if ":" in reg_part[:80]:
            reg_part = reg_part.split(":", 1)[1].strip()

        regimen = _extract_regimen(reg_part)
        if not regimen:
            cand = strip_trailing_punct(norm_spaces(reg_part))
            cand = cand.split(",", 1)[0].strip()
            if cand and ("+" in cand or _has_drug_phrase(cand) or re.search(r"\b[A-Z]{2,}\b", cand)):
                if not _is_bad_regimen(cand):
                    regimen = cand[:140]

        if regimen and _is_bad_regimen(regimen):
            regimen = None

        if not regimen and not start and not end:
            continue

        span = t[max(0, m.start() - 80) : min(len(t), m.end() + 160)]
        rows.append(
            TherapyLine(
                line=line,
                kind="THERAPY",
                regimen=regimen,
                start=start,
                end=end,
                source=norm_spaces(span),
            )
        )
        used_spans.append(sp)

    # 3) эпизоды без слова "линия": якоримся на диапазоны дат "с ... по ..."
    used_spans2: List[Tuple[int, int]] = used_spans[:]

    def overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
        return not (a[1] <= b[0] or b[1] <= a[0])

    def _handle_unlined_range(m: re.Match[str]) -> None:
        sp = m.span()
        if any(overlaps(sp, usp) for usp in used_spans2):
            return

        start = date_to_iso_like(m.group("start"))
        end = date_to_iso_like(m.group("end"))

        ctx_start = max(0, m.start() - 220)
        ctx_end = min(len(t), m.end() + 280)
        ctx = t[ctx_start:ctx_end]

        # фильтр: пропускаем интервалы, не похожие на лечение
        if not RE_THERAPY_HINT.search(ctx) and not _has_drug_phrase(ctx):
            return
        # не режем по "операция" слишком далеко: проверяем локально вокруг диапазона
        rel_start = m.start() - ctx_start
        rel_end = m.end() - ctx_start
        local_small = ctx[max(0, rel_start - 90) : min(len(ctx), rel_end + 140)]
        if RE_NOT_SYSTEMIC.search(local_small) and not _has_drug_phrase(local_small):
            return

        regimen = _extract_regimen_near_range(ctx, rel_start, rel_end)

        local = ctx[max(0, rel_start - 140) : min(len(ctx), rel_end + 180)]
        kind = _guess_kind(local)

        if regimen is None:
            # не плодим пустые эпизоды без режима: нужен явный препарат/протокол или признаки комбинации
            if (not _has_drug_phrase(local)) and ("+" not in local) and (not re.search(r"\b[A-Z]{2,}\b", local)):
                return
        elif _is_bad_regimen(regimen):
            regimen = None
            if not _has_drug_phrase(local):
                return

        rows.append(
            TherapyLine(
                line=None,
                kind=kind,
                regimen=regimen,
                start=start,
                end=end,
                source=norm_spaces(ctx),
            )
        )
        used_spans2.append(sp)


    for m in RE_ANY_RANGE.finditer(t):
        _handle_unlined_range(m)

    # диапазон без "с/по": "01.2022-10.2024" / "08.24—03.25"
    for m in RE_ANY_RANGE_BARE.finditer(t):
        _handle_unlined_range(m)

    # диапазон месяцев в скобках: "(03-05.2025)" — часто в нео-/адъювантных схемах и протоколах
    for m in RE_PAREN_MONTHSPAN.finditer(t):
        sp = m.span()
        if any(overlaps(sp, usp) for usp in used_spans2):
            continue

        y = m.group('y')
        m1 = int(m.group('m1')); m2 = int(m.group('m2'))
        if not (1 <= m1 <= 12 and 1 <= m2 <= 12):
            continue
        start = f"{y}-{m1:02d}"
        end = f"{y}-{m2:02d}"

        ctx_start = max(0, m.start() - 240)
        ctx_end = min(len(t), m.end() + 320)
        ctx = t[ctx_start:ctx_end]

        # должны быть признаки терапии (иначе это может быть просто интервал наблюдения)
        if not RE_THERAPY_HINT.search(ctx) and not _has_drug_phrase(ctx) and '+' not in ctx:
            continue

        rel_start = m.start() - ctx_start
        rel_end = m.end() - ctx_start
        local_small = ctx[max(0, rel_start - 120) : min(len(ctx), rel_end + 180)]
        if RE_NOT_SYSTEMIC.search(local_small) and not _has_drug_phrase(local_small):
            continue

        regimen = _extract_regimen_near_range(ctx, rel_start, rel_end)
        local = ctx[max(0, rel_start - 160) : min(len(ctx), rel_end + 220)]
        kind = _guess_kind(local)

        if regimen is None:
            # если не нашли режим, оставим эпизод только если в локальном окне явно есть препарат/протокол или '+'
            if (not _has_drug_phrase(local)) and ("+" not in local) and (not re.search(r"\b[A-Z]{2,}\b", local)):
                continue
        elif _is_bad_regimen(regimen):
            regimen = None
            if not _has_drug_phrase(local):
                continue

        rows.append(
            TherapyLine(
                line=None,
                kind=kind,
                regimen=regimen,
                start=start,
                end=end,
                source=norm_spaces(ctx),
            )
        )
        used_spans2.append(sp)


    # одиночная дата в скобках: "(09.2025)" — часто в "Индукция (09.2025): ..."
    for m in RE_PAREN_DATE.finditer(t):
        sp = m.span()
        if any(overlaps(sp, usp) for usp in used_spans2):
            continue
        date_iso = date_to_iso_like(m.group('date'))

        ctx_start = max(0, m.start() - 240)
        ctx_end = min(len(t), m.end() + 320)
        ctx = t[ctx_start:ctx_end]
        # должны быть признаки терапии и/или препарат рядом
        if not RE_THERAPY_HINT.search(ctx) and not _has_drug_phrase(ctx) and '+' not in ctx:
            continue

        rel_start = m.start() - ctx_start
        rel_end = m.end() - ctx_start
        local_small = ctx[max(0, rel_start - 120) : min(len(ctx), rel_end + 180)]
        if RE_NOT_SYSTEMIC.search(local_small) and not _has_drug_phrase(local_small):
            continue

        regimen = _extract_regimen_near_range(ctx, rel_start, rel_end)
        local = ctx[max(0, rel_start - 160) : min(len(ctx), rel_end + 220)]
        kind = _guess_kind(local)

        if regimen is None:
            # не плодим пустые эпизоды: нужен явный препарат/протокол или '+'
            if (not _has_drug_phrase(local)) and ("+" not in local) and (not re.search(r"\b[A-Z]{2,}\b", local)):
                continue
        elif _is_bad_regimen(regimen):
            regimen = None
            if not _has_drug_phrase(local):
                continue

        rows.append(
            TherapyLine(
                line=None,
                kind=kind,
                regimen=regimen,
                start=date_iso,
                end=None,
                source=norm_spaces(ctx),
            )
        )
        used_spans2.append(sp)
    # "... до ДАТА" без слова "линия" (например: пеметрексед 3 курса до 30.09.2025)
    for m in RE_THERAPY_UNTIL.finditer(t):
        end = date_to_iso_like(m.group("date"))
        ctx_start = max(0, m.start() - 240)
        ctx_end = min(len(t), m.end() + 80)
        ctx = t[ctx_start:ctx_end]

        # локальная проверка на "лучевая/операция" рядом с 'до'
        local_small = ctx[max(0, (m.start() - ctx_start) - 90) : min(len(ctx), (m.end() - ctx_start) + 140)]
        if RE_NOT_SYSTEMIC.search(local_small):
            continue
        if not RE_THERAPY_HINT.search(ctx) and not _has_drug_phrase(ctx):
            continue

        # попытаемся выделить режим из левого хвоста перед "до"
        left_tail = strip_trailing_punct(norm_spaces(t[ctx_start:m.start()][-180:]))
        regimen = None
        if _has_drug_phrase(left_tail):
            clause = re.split(r"[\n\r.;]", left_tail)[-1]
            clause = strip_trailing_punct(norm_spaces(clause))
            clause = re.sub(r"\b(?:мхт|хт|хтт|ит|пхт|монотерап\w*|терап\w*|курс\w*|цик\w*)\b", "", clause, flags=re.IGNORECASE)
            clause = clause.strip(" -—:;,")
            if clause and not _is_bad_regimen(clause):
                regimen = clause[:140]

        local = ctx
        if regimen is None and not _has_drug_phrase(local):
            continue

        kind = _guess_kind(local)
        rows.append(
            TherapyLine(
                line=None,
                kind=kind,
                regimen=regimen,
                start=None,
                end=end,
                source=norm_spaces(ctx),
            )
        )

    # сортировка: сначала числовые линии, потом эпизоды без номера
    rows.sort(key=lambda r: ((r.line if r.line is not None else 10_000), sort_key_date(r.start)))
    return rows