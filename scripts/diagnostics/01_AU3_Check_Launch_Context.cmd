@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "OUTPUT_DIR=%USERPROFILE%\Desktop\AU3_Diagnostic_Output"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%01_AU3_Check_Launch_Context.ps1" -OutputRoot "%OUTPUT_DIR%" -NoPause

echo.
if errorlevel 1 (
    echo Diagnostic collection failed.
) else (
    echo Diagnostic collection completed.
    echo Output folder: %OUTPUT_DIR%
    explorer.exe "%OUTPUT_DIR%"
)

echo.
pause
