@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "OUTPUT_DIR=%USERPROFILE%\Desktop\RIL_AU_Alignment_Diagnostic"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%05_RIL_AU_Alignment_Diagnostic.ps1" -OutputRoot "%OUTPUT_DIR%" -NoPause
set "RESULT=%ERRORLEVEL%"

echo.
if not "%RESULT%"=="0" (
    echo Diagnostic failed. Read the error above.
) else (
    echo Diagnostic completed.
    echo Output folder: %OUTPUT_DIR%
    explorer.exe "%OUTPUT_DIR%"
)

echo.
pause
exit /b %RESULT%
