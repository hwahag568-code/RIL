@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%03_AU3_Restore_Order_Coherent_Set.ps1" -NoPause
set "RESULT=%ERRORLEVEL%"

echo.
if not "%RESULT%"=="0" (
    echo Restore failed. Read the error above. No database file was changed.
) else (
    echo Restore script completed.
)

echo.
pause
exit /b %RESULT%
