@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   🎵 ЗАПУСК LO-FI MIXER
echo ==========================================
echo.
python run_mixer.py
echo.
pause
