@echo off
setlocal
title EchoGrab Launcher

set "PS_SCRIPT=%~dp0script\build_windows.ps1"

if not exist "%PS_SCRIPT%" (
    echo [ERROR] Could not find "%PS_SCRIPT%"
    echo Please ensure build_windows.ps1 and shazam_ytdlp_studio.py are inside the "script" folder.
    pause
    exit /b 1
)

:: Run build_windows.ps1 in CheckAndRun mode:
:: - If EchoGrab.exe exists, it checks PyPI for shazamio / yt-dlp / yt-dlp-ejs updates.
:: - If up-to-date, it launches EchoGrab.exe immediately and closes this window.
:: - If missing or outdated, it deletes the old binary, rebuilds with the latest packages, and launches it.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" -CheckAndRun
if errorlevel 1 (
    echo.
    echo [ERROR] Build or update process encountered an error.
    pause
    exit /b 1
)
exit /b 0