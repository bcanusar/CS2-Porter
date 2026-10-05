@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0CS2 Porter.pyw"
) else (
    python "%~dp0CS2 Porter.pyw"
    if errorlevel 1 pause
)
