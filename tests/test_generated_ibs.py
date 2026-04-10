from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List

import pytest


def find_project_root(start: Path) -> Path:
    """Ищем корень проекта максимально устойчиво."""
    for p in [start] + list(start.parents):
        if (p / "schemas" / "container.schema.json").exists() and (p / "src").exists():
            return p
    # запасной вариант
    return start.parents[1]


ROOT = find_project_root(Path(__file__).resolve())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.extract_text import extract_text  # noqa: E402
from src.normalize_med_text import apply_replacements  # noqa: E402
from src.coverage_layer import build_coverage_layer, quality_check_coverage  # noqa: E402
from src.rules_to_case import build_case_from_rules  # noqa: E402
from src.rules.patient_context import fill_patient_context_inplace  # noqa: E402
from src.main import validate_or_raise, ensure_minitems_lists  # noqa: E402
from src.qc_validate import validate_case  # noqa: E402


TEST_CASES_DIR = ROOT / "data" / "test_cases"
TEMPLATE_PATH = ROOT / "examples" / "case_empty.json"
SCHEMA_PATH = ROOT / "schemas" / "container.schema.json"


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _iter_ib_files() -> List[Path]:
    if not TEST_CASES_DIR.exists():
        return []
    exts = {".docx", ".pdf", ".txt"}
    files = [p for p in TEST_CASES_DIR.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    files = sorted(files)

    limit = int(os.getenv("IB_LIMIT", "0"))
    if limit > 0:
        files = files[:limit]

    return files


def _safe_name(p: Path) -> str:
    s = p.stem
    s = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", s)
    return s[:120] if len(s) > 120 else s


# --------- выгрузка артефактов ---------
RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
DUMP_DIR_RAW = os.getenv("IB_DUMP_DIR", "").strip()
DUMP_MODE = os.getenv("IB_DUMP_MODE", "always").strip().lower()  # always|fail

DUMP_ROOT: Path | None
if DUMP_DIR_RAW:
    dump_path = Path(DUMP_DIR_RAW)
    if not dump_path.is_absolute():
        dump_path = ROOT / dump_path
    DUMP_ROOT = dump_path / f"run_{RUN_STAMP}"
    DUMP_ROOT.mkdir(parents=True, exist_ok=True)
else:
    DUMP_ROOT = None


def _dump_case_artifacts(
    *,
    case_id: str,
    ib_path: Path,
    extracted_text: str,
    coverage: dict,
    case: dict,
    qc: dict,
) -> None:
    if DUMP_ROOT is None:
        return

    out_dir = DUMP_ROOT / case_id
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "source_file.txt").write_text(str(ib_path), encoding="utf-8")
    (out_dir / "extracted.txt").write_text(extracted_text, encoding="utf-8")
    (out_dir / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "case.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "qc_report.json").write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")


IB_FILES = _iter_ib_files()


@pytest.mark.parametrize("ib_path", IB_FILES, ids=lambda p: p.name)
def test_generated_ib_end_to_end(ib_path: Path) -> None:
    """Сквозной автотест: ИБ -> текст -> coverage -> case -> schema -> QC (без blocker)."""
    if not IB_FILES:
        pytest.skip(f"Нет файлов ИБ в {TEST_CASES_DIR}")

    template = _load_json(TEMPLATE_PATH)
    schema = _load_json(SCHEMA_PATH)

    extracted = extract_text(str(ib_path), clinical=True)

    # Явный детектор скан-PDF
    if extracted.file_type == "pdf" and extracted.text.startswith("[WARNING]"):
        raise AssertionError(f"PDF похоже скан/картинки (нужен OCR): {ib_path}")

    raw_text = apply_replacements(extracted.text)
    assert len(raw_text) > 200, f"Слишком мало текста (возможно скан без OCR): {ib_path}"

    coverage = build_coverage_layer(
        raw_text=raw_text,
        clean_text=raw_text,
        cleaner_version="v1.3",
        lang="ru",
        source_type=getattr(extracted, "file_type", "text"),
    )
    cov_rep = quality_check_coverage(coverage)
    assert cov_rep.get("ok") is True, f"Coverage issues: {cov_rep}"

    case_id = _safe_name(ib_path)
    case = build_case_from_rules(text=raw_text, full_text=raw_text, template=template, case_id=case_id)

    fill_patient_context_inplace(case, full_text=raw_text, broad=True)
    ensure_minitems_lists(case, template)

    validate_or_raise(case, schema)

    # --------- strict quality-gates (anti-hallucination) ---------
    th = case.get("treatment_history")
    if isinstance(th, list) and len(th) == 1 and isinstance(th[0], dict):
        x = th[0]
        if (x.get("line") is None and x.get("regimen_name") is None and not x.get("drugs")):
            raise AssertionError("treatment_history содержит placeholder (лучше пустой список, чем заглушка)")

    # biomarker: запрещаем value_norm и голые positive/negative, если их нет в тексте
    low_text = raw_text.lower()
    for bm in (case.get("biomarkers") or []):
        if not isinstance(bm, dict):
            continue
        if "value_norm" in bm:
            raise AssertionError("biomarkers: найдено derived поле value_norm (в строгом режиме запрещено)")
        v = str(bm.get("value") or "").strip().lower()
        if v in {"positive", "negative", "unknown"} and v not in low_text:
            raise AssertionError(f"biomarkers: value='{v}' не подтверждается текстом (нет буквального совпадения)")

    # allergy: severity только при явном тексте
    for al in (((case.get("patient") or {}).get("allergies") or [])):
        if not isinstance(al, dict):
            continue
        sev = al.get("severity")
        if sev is None:
            continue
        src = (al.get("source") or "").lower()
        if not re.search(r"\b(л[её]гк\w+|умерен\w+|тяж[её]л\w+)\b", src):
            raise AssertionError("allergies: severity заполнена без явного 'лёгкая/умеренная/тяжёлая' в source")

    qc = validate_case(text=raw_text, case=case)
    blockers = [i for i in qc.get("issues", []) if isinstance(i, dict) and i.get("severity") == "blocker"]

    # выгрузка артефактов
    if DUMP_ROOT is not None and DUMP_MODE in {"always", "fail"}:
        if DUMP_MODE == "always" or blockers:
            _dump_case_artifacts(
                case_id=case_id,
                ib_path=ib_path,
                extracted_text=raw_text,
                coverage=coverage,
                case=case,
                qc=qc,
            )

    assert not blockers, f"QC blockers: {blockers}"
