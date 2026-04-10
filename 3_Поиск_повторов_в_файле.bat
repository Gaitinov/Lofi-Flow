@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lo-Fi Flow: Поиск повторов
color 0E
echo ===================================================
echo   ПОИСК ПОВТОРОВ В ФАЙЛЕ (Duplicate Finder)
echo ===================================================
echo.
python scripts\find_repeats.py
echo.
pause
