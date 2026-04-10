from __future__ import annotations

import json
from pathlib import Path

from src.rules.demographics import infer_sex
from src.rules.comorbidities import extract_comorbidities
from src.main import normalize_biomarkers_inplace
from src.rules_to_case import build_case_from_rules


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "examples" / "case_empty.json"


def _template() -> dict:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def test_sex_not_inferred_from_patient_words() -> None:
    assert infer_sex("Пациент 55 лет. Диагноз: рак.") is None
    assert infer_sex("Пациентка 55 лет. Диагноз: рак.") is None
    assert infer_sex("Пол: женский. Диагноз: рак.") == "F"
    assert infer_sex("Пол: М. Диагноз: рак.") == "M"


def test_biomarker_value_not_overwritten_by_status_norm() -> None:
    data = {
        "biomarkers": [
            {
                "name_raw": "BRCA1",
                "name_std": "brca1",
                "value": "мутации не обнаружены",
                "source": "МГИ от 01.02.2024: мутации не обнаружены",
            }
        ]
    }
    normalize_biomarkers_inplace(data, "МГИ от 01.02.2024: мутации не обнаружены")
    bm = data["biomarkers"][0]
    assert bm["value"] == "мутации не обнаружены"
    # строгий режим: никаких derived-полей и статусов 'positive/negative' в value
    assert "value_norm" not in bm
    assert "status_norm=" not in (bm.get("source") or "")


def test_case0008_stage_therapy_and_allergies_strict() -> None:
    """Регрессия по audit_outputs2: stage/therapy/allergy не должны теряться/портиться."""
    template = _template()
    root = ROOT / "data" / "outputs" / "case_0008"
    text = (root / "extracted.txt").read_text(encoding="utf-8")

    case = build_case_from_rules(text=text, full_text=text, template=template, case_id="case_0008")

    # стадия должна браться из строки диагноза, а не теряться
    assert (case["diagnoses"][0].get("stage") or "").upper() == "IV"

    # терапия: "Лечение: 1 линия (07-12.2025): ..." должна попасть в treatment_history
    th = case.get("treatment_history") or []
    assert isinstance(th, list) and th, "treatment_history пустой — вероятно, не распарсили 'Лечение: 1 линия (...)'"
    first = th[0]
    assert first.get("line") == 1
    assert first.get("start_date") == "2025-07"
    assert first.get("end_date") == "2025-12"
    rn = (first.get("regimen_name") or "").lower()
    assert ("ирино" in rn) or ("folfirinox" in rn)

    # аллергии: "Нет данных о лекарственных аллергиях" не должно порождать запись
    allergies = ((case.get("patient") or {}).get("allergies") or [])
    assert allergies == []

    # биомаркеры: никакого value=positive/negative и никаких derived полей
    for bm in (case.get("biomarkers") or []):
        if not isinstance(bm, dict):
            continue
        v = (bm.get("value") or "")
        assert str(v).lower() not in {"positive", "negative", "unknown"}
        assert "value_norm" not in bm


def test_extracts_metastases_procedures_radiotherapy_conservatively() -> None:
    template = _template()
    text = """Диагноз: рак.
Прогрессирование от 08.2021: мтс в печень.
В 2017 выполнена мастэктомия слева.
В 2018 проведена лучевая терапия на область грудной стенки.
"""

    case = build_case_from_rules(text=text, full_text=text, template=template, case_id="t1")

    assert isinstance(case.get("metastases"), list) and len(case["metastases"]) >= 1
    assert "печ" in (case["metastases"][0].get("site") or "").lower()

    assert isinstance(case.get("procedures"), list) and len(case["procedures"]) >= 1
    assert "мастэктом" in (case["procedures"][0].get("name") or "").lower()

    assert isinstance(case.get("radiotherapy"), list) and len(case["radiotherapy"]) >= 1
    rt = case["radiotherapy"][0]
    assert set(["site","start_date","end_date","technique","source"]).issubset(rt.keys())
    src = (rt.get("source") or "").lower()
    assert ("лучев" in src) or ("радио" in src) or ("лт" in src)


def test_comorbidities_diverticula_literal_no_domysel() -> None:
    """Регрессия по case_0001: дивертикулы должны быть буквальными, без "дивертикулит" и без severity."""
    text = (
        "КТ: Дивертикул 12перстной кишки. Дивертикулы сигмовидной кишки. "
        "Прогрессирование: мтс в л/у корня легкого."
    )
    items = extract_comorbidities(text, include_weak_mentions=True)
    names = [str(x.get("name") or "") for x in items if isinstance(x, dict)]
    low = " | ".join(names).lower()
    assert "дивертикулит" not in low
    assert any("12" in n and "перст" in n.lower() for n in names)
    assert any("сигмов" in n.lower() for n in names)
    for x in items:
        if not isinstance(x, dict):
            continue
        if "дивертикул" in (x.get("name") or "").lower():
            assert x.get("severity") is None


def test_metastasis_date_not_confused_with_earlier_year() -> None:
    template = _template()
    text = """Диагноз: рак.
Прогрессирование от 08.2021 (локальный рецидив + мтс в л/у корня легкого).
В 2017 выполнена мастэктомия слева.
"""
    case = build_case_from_rules(text=text, full_text=text, template=template, case_id="t_mts")
    mts = case.get("metastases") or []
    assert isinstance(mts, list) and mts
    # дата должна браться из прогрессирования, а не из 2017
    assert (mts[0].get("date") or "") == "2021-08"


def test_radiotherapy_range_sets_end_date() -> None:
    template = _template()
    text = """Проведена стереотаксическая лучевая терапия с 03.06.2025 по 05.06.2025 на очаг.
"""
    case = build_case_from_rules(text=text, full_text=text, template=template, case_id="t_rt")
    rt = case.get("radiotherapy") or []
    assert isinstance(rt, list) and rt
    assert rt[0].get("start_date") == "2025-06-03"
    assert rt[0].get("end_date") == "2025-06-05"


def test_procedure_implant_removal_captured_with_date() -> None:
    template = _template()
    text = """Удаление импланта левой м/ж от 18.09.2023 выполнено.
"""
    case = build_case_from_rules(text=text, full_text=text, template=template, case_id="t_proc")
    procs = case.get("procedures") or []
    assert isinstance(procs, list) and procs
    assert any((p.get("date") == "2023-09-18") for p in procs if isinstance(p, dict))
