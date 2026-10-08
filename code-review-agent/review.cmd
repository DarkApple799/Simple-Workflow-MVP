@echo off
rem ---------------------------------------------------------------
rem  Review any project without changing directory:
rem    review.cmd                     review current directory
rem    review.cmd "D:\proj"           review the given directory
rem    review.cmd "D:\proj" scan      static rules only (offline, free)
rem    review.cmd "D:\proj" --focus security --offline
rem  (Kept PURE ASCII so cmd.exe never mis-parses it.)
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
python "%~dp0review.py" %*
set RC=%ERRORLEVEL%
echo.
echo [exit code %RC%]  0=ok  1=usage/config  2=auth  3=fail-on threshold hit
pause
endlocal
