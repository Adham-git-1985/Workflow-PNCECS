@echo off
REM Run from project root (where app.py exists)

set EXCEL_PATH=قائمة الموظفين معدلة.xlsx
set DB_PATH=instance\workflow.db

REM Mixed domains example:
set DEFAULT_DOMAIN=gmail.com
set INTERNAL_DOMAIN=pncecs.plo.ps
set INTERNAL_HINTS=pncecs

REM Fixed password for all:
set PASS=123

REM Pick python from venv if exists
set PY=.\.venv\Scripts\python.exe
if not exist "%PY%" set PY=.\venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo === DRY RUN ===
"%PY%" tools\import_employees_excel.py --excel "%EXCEL_PATH%" --db "%DB_PATH%" --dry-run
echo.

echo === APPLY IMPORT (update email + reset password) ===
"%PY%" tools\import_employees_excel.py --excel "%EXCEL_PATH%" --db "%DB_PATH%" --email-domain "%DEFAULT_DOMAIN%" --internal-domain "%INTERNAL_DOMAIN%" --internal-hints "%INTERNAL_HINTS%" --password "%PASS%" --update-email --reset-password

pause
