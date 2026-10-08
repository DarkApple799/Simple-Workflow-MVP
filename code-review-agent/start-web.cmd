@echo off
rem ---------------------------------------------------------------
rem  Code Review Agent - one click Web UI launcher
rem  (This file is kept PURE ASCII on purpose: a .cmd containing
rem   non-ASCII text can be mis-parsed by cmd.exe under a GBK code page.)
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo   Code Review Agent - Web UI
echo   Working dir : %CD%
echo   Open in browser: http://127.0.0.1:8765   (port is required)
echo ==========================================================
echo.
python webui.py %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo [ERROR] exit code %RC%
  echo   1^) Python not found or not in PATH - try: py webui.py
  echo   2^) Port already in use - try: python webui.py --port 9000
  echo   3^) Missing .env - copy .env.example to .env and set DEEPSEEK_API_KEY
)
echo.
pause
endlocal
