"""Автоматическая проверка качества case.json относительно исходного текста.

Цель: поймать типовые дефекты правил (регулярок) ДО того, как данные пойдут
в проверку клин. рекомендаций.

Запуск:
  python -m src.qc_validate --text data/outputs/case_0001/extracted.txt --case data/outputs/case_0001/case.json --out qc_report.json

Можно использовать и в пайплайне после построения case.json.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .rules.biomarkers import extract_biomarkers


# ----------------------------
# Утилиты
# ----------------------------

RE_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
RE_DOB_SAFE = re.compile(
    r"(?i)(?:Дата\s*рождения|дата\s*рожд\.?|д/р|др|DOB)\s*(?:[:\-–—])?\s*"
    r"(\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2}))(?!\d)"
)

NEG_RX = re.compile(
    r"(?i)\b(не\s+(?:выявлен\w+|получен\w+|отмечен\w+|зарегистрирован\w+|обнаружен\w+|установлен\w+)"
    r"|без\s+(?:признак\w+|данн\w+)"
    r"|отрицательн\w+)\b"
)


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _read_json(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _iso_to_dmy(iso: str) -> Optional[str]:
    m = RE_ISO_DATE.match(iso or "")
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{d}.{mo}.{y}"


def _parse_dob_from_text(text: str) -> Optional[str]:
    m = RE_DOB_SAFE.search((text or "")[:12000])
    if not m:
        return None
    s = m.group(1)
    dd, mm, yy = s.split(".")
    dd_i, mm_i = int(dd), int(mm)
    yy_i = int(yy)
    if len(yy) == 2:
        yy_i = (2000 + yy_i) if yy_i <= 30 else (1900 + yy_i)
    try:
        return date(yy_i, mm_i, dd_i).isoformat()
    except ValueError:
        return None


def _infer_sex_from_text(text: str) -> Optional[str]:
    head = (text or "")[:12000].lower()
    if re.search(r"\bпол\s*[:\-]?\s*жен", head):
        return "F"
    if re.search(r"\bпол\s*[:\-]?\s*муж", head):
        return "M"
    if "пациентка" in head:
        return "F"
    if re.search(r"\bпациент\b", head):
        return "M"
    return None


def _find_occurrences(text: str, rx: re.Pattern[str], window: int = 80) -> List[str]:
    out: List[str] = []
    t = text or ""
    for m in rx.finditer(t):
        a, b = m.span()
        lo = max(0, a - window)
        hi = min(len(t), b + window)
        out.append(t[lo:hi])
    return out


# ----------------------------
# Формат отчёта
# ----------------------------


@dataclass
class Issue:
    id: str
    severity: str  # blocker|warning|info
    message: str
    details: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        d = {"id": self.id, "severity": self.severity, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d


def validate_case(*, text: str, case: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    today = today or date.today()
    issues: List[Issue] = []

    def add(i: Issue) -> None:
        issues.append(i)

    # 1) Демография
    demo = ((case.get("patient") or {}).get("demographics") or {}) if isinstance(case, dict) else {}
    dob_json = demo.get("dob")
    dob_text = _parse_dob_from_text(text)
    if dob_json and RE_ISO_DATE.match(str(dob_json)):
        age = today.year - int(str(dob_json)[:4])
        if age < 10 or age > 120:
            add(Issue(
                id="patient.demographics.dob.range",
                severity="blocker",
                message=f"DOB выглядит неправдоподобно: {dob_json} (возраст ~{age}). Лучше ставить null, чем неверный год.",
                details={"dob_json": dob_json, "dob_text": dob_text},
            ))
    if dob_text and dob_json and dob_text != dob_json:
        add(Issue(
            id="patient.demographics.dob.mismatch",
            severity="blocker",
            message=f"DOB не совпадает с текстом: в JSON {dob_json}, в тексте {dob_text}.",
            details={"dob_json": dob_json, "dob_text": dob_text},
        ))

    sex_json = demo.get("sex")
    sex_text = _infer_sex_from_text(text)
    if sex_text and sex_json and sex_text != sex_json:
        add(Issue(
            id="patient.demographics.sex.mismatch",
            severity="warning",
            message=f"Пол в JSON ({sex_json}) расходится с эвристикой по тексту ({sex_text}).",
        ))
    if not sex_json:
        add(Issue(id="patient.demographics.sex.missing", severity="info", message="Пол не заполнен."))

    # 2) Биомаркеры: полнота относительно полного текста
    bms_case = case.get("biomarkers") or []
    case_std = {b.get("name_std") for b in bms_case if isinstance(b, dict) and b.get("name_std")}
    bms_full = extract_biomarkers(text)
    full_std = {b.name_std for b in bms_full}
    missing = sorted(full_std - case_std)
    if missing:
        add(Issue(
            id="biomarkers.missing_from_case",
            severity="warning",
            message="В полном тексте правила находят больше биомаркеров, чем попало в case.json (вероятно из-за focus.txt).",
            details={"missing": missing},
        ))

    # 3) Простейшая проверка отрицаний для коморбидности (точечная, но полезная)
    comorbs = ((case.get("patient") or {}).get("comorbidities") or [])
    if isinstance(comorbs, list):
        for idx, c in enumerate(comorbs):
            if not isinstance(c, dict):
                continue
            name = (c.get("name") or "").strip()
            if not name:
                continue
            # минимальная эвристика: если совпадение встречается только с отрицанием — это ложноположительное
            # (список расширяйте по мере накопления кейсов).
            if name in {"инсульт/ОНМК в анамнезе", "тромбоз/ТЭЛА"}:
                rx = re.compile(r"(?i)\bОНМК\b|инсульт\w+" if "ОНМК" in name else r"(?i)\bТЭЛА\b|тромбоз\w+")
                occ = _find_occurrences(text, rx, window=90)
                if occ and all(NEG_RX.search(x) for x in occ):
                    add(Issue(
                        id="patient.comorbidities.negation.false_positive",
                        severity="warning",
                        message=f"Коморбидность '{name}' встречается только в отрицательном контексте (вероятный ложноположительный).",
                        details={"index": idx, "occurrences": len(occ)},
                    ))

    # итоговый балл (простая формула)
    sev_w = {"blocker": 5, "warning": 2, "info": 1}
    score = 100 - 3 * sum(sev_w.get(i.severity, 1) for i in issues)
    score = max(0, min(100, score))

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "score": score,
        "issues": [i.as_dict() for i in issues],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="Путь к extracted.txt (полный текст)")
    ap.add_argument("--case", required=True, help="Путь к case.json")
    ap.add_argument("--out", required=True, help="Куда записать qc_report.json")
    args = ap.parse_args()

    text = _read_text(Path(args.text))
    case = _read_json(Path(args.case))
    rep = validate_case(text=text, case=case)
    Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print("OK:", args.out)


if __name__ == "__main__":
    main()
