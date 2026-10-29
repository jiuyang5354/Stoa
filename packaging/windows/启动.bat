@echo off
chcp 65001 >nul
title Stoa
set PYTHONUTF8=1
cd /d "%~dp0"
set STOA_LOG=0
python\python.exe -m stock_agent desktop
if errorlevel 1 pause
