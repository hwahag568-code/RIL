@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%04_AU3_Rollback_Last_Order_Restore.ps1" -NoPause
set "RESULT=%ERRORLEVEL%"

echo.
if not "%RESULT%"=="0" (
    echo Rollback failed. Read the error above.
) else (
    echo Rollback script completed.
)

echo.
pause
exit /b %RESULT%
