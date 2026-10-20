@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0OpsPilot.exe" (
    echo OpsPilot.exe was not found.
    echo Please keep this launcher in the OpsPilot project folder.
    pause
    exit /b 1
)

if not exist "%~dp0config.yaml" (
    echo config.yaml was not found.
    pause
    exit /b 1
)

"%~dp0OpsPilot.exe" --config "%~dp0config.yaml"

echo.
echo OpsPilot has stopped.
pause
