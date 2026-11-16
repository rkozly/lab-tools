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
    REM keep it only if it runs, has pip, and was not built from the Microsoft Store Python
    "%SYS%\findstr.exe" /i "WindowsApps" "%VENV%\pyvenv.cfg" >nul 2>&1 || (
        "%PY%" -m pip --version >nul 2>&1 && exit /b 0
    )
    echo Existing environment is broken or uses the Microsoft Store Python - recreating...
    rmdir /s /q "%VENV%"
)
echo Creating virtual environment in %VENV% ...
REM The Python that builds the venv: 3.12 first; never the Microsoft Store one (...\WindowsApps\...).
REM `py -3` alone would take the newest 3.x the launcher knows - that can be the Store's.
set "BASEPY="
for /f "delims=" %%P in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do call :use_python "%%P"
for %%P in ("C:\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do call :use_python "%%~P"
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do call :use_python "%%P"
for /f "delims=" %%P in ('where python 2^>nul') do call :use_python "%%P"
if not defined BASEPY (
    echo ERROR: no usable Python found ^(the Microsoft Store Python is not used^).
    echo        Install Python 3.12 from https://www.python.org/downloads/
    echo        ^(tick "Add python.exe to PATH" and "pip"^) and run LabHub.bat again.
    exit /b 1
)
echo Using Python: %BASEPY%
echo base python: %BASEPY% >> "%LOG%"
REM --without-pip: some Python installs have no ensurepip, and then venv fails half way.
"%BASEPY%" -m venv --without-pip "%VENV%" >>"%LOG%" 2>&1
if not exist "%PY%" (
    echo ERROR: could not create the virtual environment with %BASEPY%
    exit /b 1
)
REM pip for the venv: 1) ensurepip  2) the base Python's own pip  3) get-pip.py from the internet
"%PY%" -m ensurepip --upgrade >>"%LOG%" 2>&1
"%PY%" -m pip --version >nul 2>&1 && exit /b 0
echo ensurepip is not available - trying the pip of %BASEPY% ...
"%BASEPY%" -m pip --python "%PY%" install --disable-pip-version-check pip >>"%LOG%" 2>&1
"%PY%" -m pip --version >nul 2>&1 && exit /b 0
echo Downloading pip ^(get-pip.py^) ...
set "GETPIP=%TEMP%\labhub-get-pip.py"
"%SYS%\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing https://bootstrap.pypa.io/get-pip.py -OutFile '%GETPIP%'" >>"%LOG%" 2>&1
if exist "%GETPIP%" "%PY%" "%GETPIP%" --disable-pip-version-check >>"%LOG%" 2>&1
"%PY%" -m pip --version >nul 2>&1 && exit /b 0
echo ERROR: pip could not be installed for %BASEPY%
echo   - reinstall Python 3.11+ from https://www.python.org/downloads/ with the "pip" option ticked, or
echo   - check the internet / proxy ^(get-pip.py could not be downloaded^)
echo Then run "LabHub.bat --reset".
exit /b 1

:use_python
REM %1 = a python.exe; becomes BASEPY if none was chosen yet, it exists, it is not the
REM Microsoft Store one, and it is Python 3.10+ with venv.
if defined BASEPY exit /b 0
if not exist "%~1" exit /b 0
echo %~1| "%SYS%\find.exe" /i "WindowsApps" >nul && exit /b 0
"%~1" -c "import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 || exit /b 0
set "BASEPY=%~1"
exit /b 0

:error
echo.
echo Failed to start LabHub. See the messages above (also in %LOG%).
pause
exit /b 1
