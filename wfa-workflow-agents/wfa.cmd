@echo off
rem ===================================================================
rem wfa 便捷启动脚本（Windows）
rem   wfa.cmd list
rem   wfa.cmd run --workflow code_review --offline --verbose
rem   wfa.cmd graph --workflow code_review --format md --out docs\FLOW.md
rem 说明：cmd 的 cd 不会切换盘符，所以这里用 cd /d 回到脚本所在目录。
rem ===================================================================
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
chcp 65001 >nul 2>nul

if "%~1"=="" (
  python run.py --help
) else (
  python run.py %*
)
endlocal
