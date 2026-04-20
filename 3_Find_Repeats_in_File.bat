@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lo-Fi Flow: Find Repeats
color 0E
echo ===================================================
echo   [ Lo-Fi High-Precision Repeat Detection ]
echo ===================================================
echo.
python scripts\find_repeats.py
echo.
pause
