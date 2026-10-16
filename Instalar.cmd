@echo off
REM Lanzador de doble clic para install.ps1.
REM -ExecutionPolicy Bypass afecta solo a este proceso: no cambia nada del sistema.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
