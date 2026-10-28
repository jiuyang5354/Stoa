@echo off
chcp 65001 >nul
title STOA Stock Agent
set PYTHONUTF8=1
cd /d "%~dp0"
python\python.exe -m stock_agent desktop
if errorlevel 1 pause
