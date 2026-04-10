@echo off
setlocal

REM 1) venv
if not exist .venv (
  python -m venv .venv
)

REM 2) deps
call .venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements.txt

REM 3) run (пример: прогон папки с кейсами)
python -m src.batch_run --in data\test_cases --out artifacts

echo.
echo Done. Results are in artifacts\
endlocal
