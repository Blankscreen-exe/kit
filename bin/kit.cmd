@echo off
rem kit launcher for cmd.exe (PowerShell uses kit.ps1 next to this file).
rem Runs kit inside the repo's uv environment so tools can use the packages in pyproject.toml.
rem Set KIT_PYTHON to skip uv and use that interpreter directly.
setlocal
set "KIT_ROOT=%~dp0.."
if defined KIT_PYTHON goto custom
where uv >nul 2>nul || goto plain
uv run --quiet --project "%KIT_ROOT%" python "%KIT_ROOT%\kit.py" %*
exit /b %ERRORLEVEL%

:custom
"%KIT_PYTHON%" "%KIT_ROOT%\kit.py" %*
exit /b %ERRORLEVEL%

:plain
python "%KIT_ROOT%\kit.py" %*
exit /b %ERRORLEVEL%
