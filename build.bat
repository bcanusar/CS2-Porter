@echo off
rem Builds build\dist\CS2 Porter.exe and release\CS2-Porter-<version>.zip.
rem Uses .venv (made on the first run) or the Python given in BUILD_PYTHON.
setlocal
cd /d "%~dp0"

if defined BUILD_PYTHON goto build
set "BUILD_PYTHON=.venv\Scripts\python.exe"
if exist "%BUILD_PYTHON%" goto build
echo Making .venv ...
py -3 -m venv .venv 2>nul || python -m venv .venv || goto fail
"%BUILD_PYTHON%" -m pip install --upgrade pip || goto fail
"%BUILD_PYTHON%" -m pip install -r requirements-build.txt || goto fail

:build
"%BUILD_PYTHON%" build\make_version.py || goto fail
"%BUILD_PYTHON%" -m PyInstaller --noconfirm --distpath build\dist --workpath build\work "build\CS2 Porter.spec" || goto fail
"%BUILD_PYTHON%" build\package.py || goto fail
echo.
echo Done.
exit /b 0

:fail
echo.
echo Build failed.
exit /b 1
