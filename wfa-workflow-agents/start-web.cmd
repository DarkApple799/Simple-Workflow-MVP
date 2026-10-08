@echo off
rem ===================================================================
rem 一键启动 Web 界面并自动打开浏览器（Windows）
rem   双击本文件，或在 cmd 中运行 start-web.cmd
rem 访问地址必须带端口：http://127.0.0.1:8765
rem ===================================================================
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
chcp 65001 >nul 2>nul

echo 正在启动 wfa Web 界面（零依赖，默认离线模式）...
echo 地址：http://127.0.0.1:8765
python webui.py --port 8765
endlocal
