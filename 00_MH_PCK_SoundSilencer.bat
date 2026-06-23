:: LAUNCHER FOR MH PCK SOUND SILENCER

@echo off
setlocal ENABLEEXTENSIONS

cd /d "%~dp0"

echo ===================================================
echo MH PCK SOUND SILENCER
echo ===================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not found in PATH.
    pause
    exit /b 1
)

python "%~dp000_MH_PCK_SoundSilencer.py"
if errorlevel 1 (
    echo.
    echo [ERROR] Setup failed. See messages above.
    pause
    exit /b 1
)

endlocal
pause
