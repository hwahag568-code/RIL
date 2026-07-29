@echo off
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0RIL_server_restarter.ps1" -InstallDir "%~dp0"
