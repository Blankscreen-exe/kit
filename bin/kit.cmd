@echo off
rem kit launcher for cmd.exe (PowerShell uses kit.ps1 next to this file).
setlocal
if defined KIT_PYTHON (set "KIT_PY=%KIT_PYTHON%") else (set "KIT_PY=python")
"%KIT_PY%" "%~dp0..\kit.py" %*
exit /b %ERRORLEVEL%
