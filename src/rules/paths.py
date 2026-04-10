from __future__ import annotations

from pathlib import Path

# Корень проекта: .../<project_root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

NOSOLOGY_ALIASES_PATH = PROJECT_ROOT / "resources" / "nosology_aliases.json"
BIOMARKERS_CATALOG_PATH = PROJECT_ROOT / "resources" / "biomarkers.json"
