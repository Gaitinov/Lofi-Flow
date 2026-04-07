@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lo-Fi Flow: Склеивание папки в микс
color 0B
echo ===================================================
echo    [ Lo-Fi Multi-Track Merger: Склейка папки ]
echo ===================================================
echo.
python merge_tracks.py
echo.
pause
