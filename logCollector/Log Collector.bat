@echo off
rem ---------------------------------------------------------------------------
rem Double-click launcher for Log Collector.
rem
rem 1. finds Python (python on PATH, or the "py" launcher)
rem 2. installs PySide6 for that Python if it is missing
rem 3. starts the GUI without a console window
rem If anything fails, this window stays open and says what to install.
rem ---------------------------------------------------------------------------
setlocal
rem pushd maps the network folder to a temporary drive letter, so cmd does
rem not complain about UNC paths.
pushd "%~dp0" 2>nul
set "APP=%~dp0logCollector.py"
set "PY="

rem --- find a real Python (skip the Microsoft Store placeholder) -----------
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY (
        echo %%P | find /i "WindowsApps" >nul || set "PY=%%P"
    )
)
if not defined PY (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
)
if not defined PY (
    echo Python is not installed on this PC.
    echo.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer.
    echo Then double-click this file again.
    goto :fail
)
echo Using Python: %PY%

rem --- make sure PySide6 and pyserial are installed for this Python --------
"%PY%" -c "import PySide6, serial" >nul 2>nul
if errorlevel 1 (
    echo PySide6 / pyserial are not installed for this Python. Installing now...
    "%PY%" -m pip install --user -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo Installing PySide6 failed. Check the internet / proxy connection,
        echo or ask IT to install it:  "%PY%" -m pip install PySide6 pyserial
        goto :fail
    )
)

rem --- start without a console window --------------------------------------
for %%D in ("%PY%") do set "PYW=%%~dpDpythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" "%APP%"
) else (
    "%PY%" "%APP%"
    if errorlevel 1 goto :fail
)
popd
exit /b 0

:fail
echo.
echo Log Collector could not be started. See the message above.
popd
pause
exit /b 1
