:: INSTALLER AND LAUNCHER FOR PCK EDITOR

@echo off
setlocal ENABLEEXTENSIONS

cd /d "%~dp0"

echo ===================================================
echo PCK EDITOR
echo ===================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not found in PATH.
    pause
    exit /b 1
)

python "%~dp001_dev_install_and_launch_workflow.py"
if errorlevel 1 (
    echo.
    echo [ERROR] Setup failed. See messages above.
    pause
    exit /b 1
)

endlocal
pause
