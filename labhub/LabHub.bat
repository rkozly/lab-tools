@echo off
REM LabHub launcher. Works from local folders and network (UNC) shares.
REM The virtual environment lives in %LOCALAPPDATA%\LabHub\venv (local disk),
REM because a venv on a network share is slow and cmd cannot cd into UNC paths.
REM (cmd prints "UNC paths are not supported" when started from a share: harmless.)
REM   LabHub.bat            normal start
REM   LabHub.bat --console  start with a console window that shows errors
REM   LabHub.bat --reset    delete and rebuild the virtual environment
REM Steps are also written to %LOCALAPPDATA%\LabHub\launcher.log
setlocal
set "SRC=%~dp0"
set "VENV=%LOCALAPPDATA%\LabHub\venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "STAMP=%VENV%\.requirements_installed"
set "LOG=%LOCALAPPDATA%\LabHub\launcher.log"
set "ERRFILE=%APPDATA%\LabHub\startup_error.txt"
set "SYS=%SystemRoot%\System32"
REM cmd cannot use a UNC folder as the current directory: work from the local disk
cd /d "%LOCALAPPDATA%" 2>nul || cd /d "%USERPROFILE%"
if not exist "%LOCALAPPDATA%\LabHub" mkdir "%LOCALAPPDATA%\LabHub"
echo %date% %time%  LabHub.bat %* > "%LOG%"

echo LabHub: %SRC%
if not exist "%SRC%run_labhub.py" (
    echo ERROR: %SRC%run_labhub.py not found - is the share reachable?
    goto :error
)
if /i "%~1"=="--reset" (
    echo Removing %VENV% ...
    rmdir /s /q "%VENV%"
)

echo [1/4] Checking Python environment...
call :ensure_venv || goto :error

echo [2/4] Checking requirements...
fc /b "%SRC%requirements.txt" "%STAMP%" >nul 2>&1
if errorlevel 1 call :install || goto :error
"%PY%" -c "import PySide6.QtWidgets, yaml, paramiko" >>"%LOG%" 2>&1
if errorlevel 1 (
    echo Requirements are broken - reinstalling...
    "%PY%" -m pip install --disable-pip-version-check --force-reinstall -r "%SRC%requirements.txt" || goto :error
    "%PY%" -c "import PySide6.QtWidgets, yaml, paramiko" || goto :error
)

echo [3/4] Checking LabHub code...
"%PY%" -c "import sys; sys.path.insert(0, r'%SRC:~0,-1%'); import labhub.ui.main_window" >>"%LOG%" 2>&1
if errorlevel 1 (
    echo ERROR: LabHub's code does not load:
    "%PY%" -c "import sys; sys.path.insert(0, r'%SRC:~0,-1%'); import labhub.ui.main_window"
    goto :error
)

if /i "%~1"=="--console" (
    echo [4/4] Starting LabHub in this window ^(errors are shown here^)...
    "%PY%" "%SRC%run_labhub.py"
    echo LabHub exited with code %errorlevel%.
    pause
    exit /b 0
)

echo [4/4] Starting LabHub...
if exist "%ERRFILE%" del "%ERRFILE%" >nul 2>&1
start "LabHub" "%PYW%" "%SRC%run_labhub.py"
REM Wait and make sure it is really running (its window is titled LabHub).
REM (ping is used as a sleep: timeout.exe fails when the input is redirected)
"%SYS%\ping.exe" -n 7 127.0.0.1 >nul
if exist "%ERRFILE%" goto :crashed
call :running
if errorlevel 1 (
    "%SYS%\ping.exe" -n 9 127.0.0.1 >nul
    if exist "%ERRFILE%" goto :crashed
    call :running
)
if errorlevel 1 (
    echo.
    echo ERROR: LabHub closed right after starting, without an error message.
    echo Running it again in this window to show the error:
    echo.
    "%PY%" "%SRC%run_labhub.py"
    goto :error
)
echo LabHub is running. This window closes by itself.
"%SYS%\ping.exe" -n 4 127.0.0.1 >nul
exit /b 0

REM ---------------------------------------------------------------------------
:crashed
echo.
echo ERROR: LabHub crashed while starting:
type "%ERRFILE%"
goto :error

:running
"%SYS%\tasklist.exe" /v /fi "imagename eq pythonw.exe" /fo csv /nh 2>nul | "%SYS%\find.exe" /i "LabHub" >nul
exit /b %errorlevel%

:install
echo Installing requirements - first run can take a few minutes...
"%PY%" -m pip install --disable-pip-version-check -r "%SRC%requirements.txt" || exit /b 1
copy /y "%SRC%requirements.txt" "%STAMP%" >nul
exit /b 0

:ensure_venv
if exist "%PY%" (
    "%PY%" -c "import sys" >nul 2>&1 && exit /b 0
    echo Existing environment does not run ^(Python was updated or removed^) - recreating...
    rmdir /s /q "%VENV%"
)
echo Creating virtual environment in %VENV% ...
py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || (
    echo ERROR: no Python found. Install Python 3.11+ from https://www.python.org/downloads/
    echo        ^(tick "Add python.exe to PATH"^) and run LabHub.bat again.
    exit /b 1
)
"%PY%" -m pip --version >nul 2>&1 && exit /b 0
"%PY%" -m ensurepip --upgrade >nul 2>&1
"%PY%" -m pip --version >nul 2>&1 && exit /b 0
echo ERROR: pip is not available for this Python installation.
echo Install Python 3.11+ from https://www.python.org/downloads/ and try again.
exit /b 1

:error
echo.
echo Failed to start LabHub. See the messages above (also in %LOG%).
pause
exit /b 1
