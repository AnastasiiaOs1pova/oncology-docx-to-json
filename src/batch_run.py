"""Batch runner for many IB files.

Запуск из корня проекта:

  python -m src.batch_run --in data/test_cases --out artifacts/ib_outputs

По умолчанию создаёт подпапку run_YYYYMMDD_HHMMSS и для каждого файла
кладёт:
  extracted.txt, coverage.json, case.json, qc_report.json

Коды возврата:
  0 — всё ок (или есть только warning/info)
  2 — есть blocker (если включён --fail-on-blocker)
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .extract_text import extract_text
from .normalize_med_text import apply_replacements
from .coverage_layer import build_coverage_layer, quality_check_coverage
from .rules_to_case import build_case_from_rules
from .rules.patient_context import fill_patient_context_inplace
from .main import validate_or_raise, ensure_minitems_lists
from .qc_validate import validate_case


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _safe_name(p: Path) -> str:
    s = p.stem
    s = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", s)
    return s[:120] if len(s) > 120 else s


def _iter_files(root: Path) -> List[Path]:
    exts = {".docx", ".pdf", ".txt"}
    if root.is_file() and root.suffix.lower() in exts:
        return [root]
    if not root.exists():
        return []
    return sorted([p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts])


@dataclass
class CaseResult:
    case_id: str
    path: str
    ok: bool
    blockers: int
    warnings: int
    infos: int
    error: Optional[str] = None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", default="data/test_cases", help="Папка с ИБ или путь к одному файлу")
    ap.add_argument("--out", dest="out_dir", default="artifacts/ib_outputs", help="Папка для выгрузки")
    ap.add_argument("--limit", type=int, default=0, help="Ограничить количество файлов (0 = без лимита)")
    ap.add_argument("--no-coverage", action="store_true", help="Не строить coverage layer")
    ap.add_argument("--no-qc", action="store_true", help="Не делать qc_validate")
    ap.add_argument("--fail-on-blocker", action="store_true", help="Вернуть код 2, если есть blocker")
    ap.add_argument("--template", default="examples/case_empty.json")
    ap.add_argument("--schema", default="schemas/container.schema.json")
    args = ap.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    in_path = Path(args.in_path)
    if not in_path.is_absolute():
        in_path = project_root / in_path

    out_root = Path(args.out_dir)
    if not out_root.is_absolute():
        out_root = project_root / out_root

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = out_root / f"run_{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    template_path = Path(args.template)
    if not template_path.is_absolute():
        template_path = project_root / template_path
    schema_path = Path(args.schema)
    if not schema_path.is_absolute():
        schema_path = project_root / schema_path

    template = _load_json(template_path)
    schema = _load_json(schema_path)

    files = _iter_files(in_path)
    if args.limit > 0:
        files = files[: args.limit]

    summary: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "input": str(in_path),
        "output": str(run_dir),
        "count": len(files),
        "results": [],
    }

    results: List[CaseResult] = []

    for p in files:
        case_id = _safe_name(p)
        out_dir = run_dir / case_id
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            extracted = extract_text(str(p), clinical=True)
            if extracted.file_type == "pdf" and extracted.text.startswith("[WARNING]"):
                raise RuntimeError("PDF похоже скан/картинки: нужен OCR")

            raw_text = apply_replacements(extracted.text)
            if len(raw_text) <= 200:
                raise RuntimeError("Слишком мало текста после извлечения")

            # всегда сохраняем текст
            (out_dir / "source_file.txt").write_text(str(p), encoding="utf-8")
            (out_dir / "extracted.txt").write_text(raw_text, encoding="utf-8")

            coverage: Optional[dict] = None
            if not args.no_coverage:
                coverage = build_coverage_layer(
                    raw_text=raw_text,
                    clean_text=raw_text,
                    cleaner_version="v1.3",
                    lang="ru",
                    source_type=getattr(extracted, "file_type", "text"),
                )
                (out_dir / "coverage.json").write_text(
                    json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                cov_rep = quality_check_coverage(coverage)
                if not cov_rep.get("ok"):
                    raise RuntimeError(f"Coverage issues: {cov_rep}")

            case = build_case_from_rules(text=raw_text, full_text=raw_text, template=template, case_id=case_id)
            fill_patient_context_inplace(case, full_text=raw_text, broad=True)
            ensure_minitems_lists(case, template)
            validate_or_raise(case, schema)
            (out_dir / "case.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")

            qc: dict = {"issues": [], "ok": True}
            blockers = warnings = infos = 0
            if not args.no_qc:
                qc = validate_case(text=raw_text, case=case)
                (out_dir / "qc_report.json").write_text(
                    json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8"
                )

                for it in qc.get("issues", []) or []:
                    if not isinstance(it, dict):
                        continue
                    sev = it.get("severity")
                    if sev == "blocker":
                        blockers += 1
                    elif sev == "warning":
                        warnings += 1
                    else:
                        infos += 1

            ok = blockers == 0
            results.append(
                CaseResult(
                    case_id=case_id,
                    path=str(p),
                    ok=ok,
                    blockers=blockers,
                    warnings=warnings,
                    infos=infos,
                )
            )

        except Exception as e:
            results.append(
                CaseResult(
                    case_id=case_id,
                    path=str(p),
                    ok=False,
                    blockers=1,
                    warnings=0,
                    infos=0,
                    error=str(e),
                )
            )
            (out_dir / "error.txt").write_text(str(e), encoding="utf-8")

    summary["results"] = [r.__dict__ for r in results]
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # короткое резюме в консоль
    blockers_total = sum(r.blockers for r in results)
    ok_total = sum(1 for r in results if r.ok)
    print(f"Done. Cases: {len(results)} | ok: {ok_total} | blockers: {blockers_total} | out: {run_dir}")

    if args.fail_on_blocker and blockers_total > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
