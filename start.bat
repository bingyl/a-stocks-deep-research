@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m uvicorn main:app --reload --reload-exclude data --reload-exclude .venv --reload-exclude logs --reload-exclude "_tmp_*" --host 127.0.0.1 --port 8000
