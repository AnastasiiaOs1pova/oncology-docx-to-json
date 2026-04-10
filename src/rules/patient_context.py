# src/rules/patient_context.py
from __future__ import annotations

from typing import Any, Dict, List

from .demographics import fill_demographics_inplace
from .comorbidities import extract_comorbidities
from .allergies import extract_allergies
from .concomitant_meds import extract_concomitant_meds


def fill_patient_context_inplace(
    data: Dict[str, Any],
    *,
    full_text: str,
    broad: bool = True,
) -> None:
    """
    Заполняет:
      patient.demographics
      patient.comorbidities
      patient.allergies
      patient.concomitant_meds

    broad=True:
      - включает weak-упоминания (помечаются confidence=weak в source)
    broad=False:
      - только “strong” секции/заголовки
    """
    if not isinstance(data, dict):
        return
    patient = data.get("patient")
    if not isinstance(patient, dict):
        return

    # demographics
    fill_demographics_inplace(data, text=full_text)

    # comorbidities
    if isinstance(patient.get("comorbidities"), list):
        patient["comorbidities"] = extract_comorbidities(full_text, include_weak_mentions=broad)

    # allergies
    if isinstance(patient.get("allergies"), list):
        patient["allergies"] = extract_allergies(full_text, include_weak_mentions=broad)

    # concomitant meds
    if isinstance(patient.get("concomitant_meds"), list):
        patient["concomitant_meds"] = extract_concomitant_meds(full_text, include_weak_mentions=broad)