@echo off
setlocal
cd /d "%~dp0"
python "%~dp0codex_history_transfer_gui.py"
if errorlevel 1 (
  echo.
  echo codex-history-transfer GUI exited with an error.
  pause
)
