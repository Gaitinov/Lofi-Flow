@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lo-Fi Flow: Process Single Track
color 0A
echo ===================================================
echo    [ Lo-Fi Single Track Mixer ]
echo ===================================================
echo.
python scripts\run_mixer.py
echo.
pause
