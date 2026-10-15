@echo off
REM Build a standalone DevReader.exe with PyInstaller -> dist\DevReader\
REM pushd maps a UNC share to a temporary drive letter (cmd cannot cd into UNC paths).
setlocal
pushd "%~dp0" || goto :error
set "VENV=%LOCALAPPDATA%\DevReader\build-venv"
if not exist "%VENV%\Scripts\python.exe" (
    py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || goto :error
)
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements-dev.txt || goto :error
"%VENV%\Scripts\python.exe" -m pytest -q -p no:cacheprovider || goto :error
"%VENV%\Scripts\python.exe" -m PyInstaller devreader.spec --noconfirm --clean ^
    --workpath "%LOCALAPPDATA%\DevReader\build-work" || goto :error
echo.
echo Build complete: %CD%\dist\DevReader\DevReader.exe
popd
exit /b 0
:error
echo Build failed.
popd
pause
exit /b 1
