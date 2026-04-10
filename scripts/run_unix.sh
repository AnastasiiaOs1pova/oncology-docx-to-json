#!/usr/bin/env bash
set -euo pipefail

# 1) venv
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

# 2) deps
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt

# 3) run
python -m src.batch_run --in data/test_cases --out artifacts

echo "Done. Results are in artifacts/"
