@echo off
REM DevReader launcher. Works from local folders and network (UNC) shares.
REM The virtual environment lives in %LOCALAPPDATA%\DevReader\venv (local disk),
REM because a venv on a network share is slow and cmd cannot cd into UNC paths.
REM (When started from Explorer on a share, cmd prints a harmless "UNC paths are
REM not supported" notice first - it can be ignored.)
setlocal
set "SRC=%~dp0"
set "VENV=%LOCALAPPDATA%\DevReader\venv"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.requirements_installed"

call :ensure_venv || goto :error

REM (Re)install requirements on first run or when requirements.txt changes.
fc /b "%SRC%requirements.txt" "%STAMP%" >nul 2>&1
if errorlevel 1 (
    echo Installing requirements - first run can take a minute...
    "%PY%" -m pip install --disable-pip-version-check -q -r "%SRC%requirements.txt" || goto :error
    copy /y "%SRC%requirements.txt" "%STAMP%" >nul
)

REM pushd maps a UNC path to a temporary drive letter so the working directory is valid.
pushd "%SRC%"
start "" "%VENV%\Scripts\pythonw.exe" "%SRC%run_devreader.py" %*
popd
exit /b 0

REM ---------------------------------------------------------------------------
REM Make sure the venv exists AND has a working pip. Repairs or rebuilds
REM environments left incomplete by an interrupted earlier run.
:ensure_venv
if exist "%PY%" (
    "%PY%" -m pip --version >nul 2>&1 && exit /b 0
    echo Existing environment is incomplete - repairing...
    "%PY%" -m ensurepip --upgrade >nul 2>&1
    "%PY%" -m pip --version >nul 2>&1 && exit /b 0
    echo Repair failed - recreating environment...
    rmdir /s /q "%VENV%"
)
echo Creating virtual environment in %VENV% ...
py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || exit /b 1
"%PY%" -m pip --version >nul 2>&1 && exit /b 0
"%PY%" -m ensurepip --upgrade >nul 2>&1
"%PY%" -m pip --version >nul 2>&1 && exit /b 0
echo ERROR: pip is not available for this Python installation.
echo Install Python 3.10+ from https://www.python.org/downloads/ ^(with pip^) and try again.
exit /b 1

:error
echo.
echo Failed to set up DevReader. See messages above.
pause
exit /b 1
