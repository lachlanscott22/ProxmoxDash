@echo off
rem Double-click or run from cmd. Passes any arguments through, e.g.  release.bat minor "Add X"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" %*
echo.
pause
