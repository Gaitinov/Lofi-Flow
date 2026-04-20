@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lo-Fi Flow: Merge Multiple Tracks
color 0B
echo ===================================================
echo    [ Lo-Fi Multi-Track Merger ]
echo ===================================================
echo.
python scripts\merge_tracks.py
echo.
pause
