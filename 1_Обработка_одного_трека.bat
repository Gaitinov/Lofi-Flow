@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lo-Fi Flow: Обработка одного трека
color 0A
echo ===================================================
echo   🎵 ЗАПУСК ОБРАБОТКИ ОДНОГО ТРЕКА (Mixer Single)
echo ===================================================
echo.
python run_mixer.py
echo.
pause
