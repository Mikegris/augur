@echo off
REM AUGUR -- start the app (Windows)
REM First-time install? Run setup.bat first.

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ====================================================
echo   AUGUR // WEALTH INTELLIGENCE SYSTEM
echo ====================================================
echo.

REM Bootstrap if venv missing
if not exist "venv" (
    echo First run -- bootstrapping via setup.bat ...
    call setup.bat
    if errorlevel 1 exit /b 1
)

call venv\Scripts\activate.bat

if "%PORT%"=="" set PORT=5001
set REQUESTED_PORT=%PORT%

REM ---- port selection -----------------------------------------------------
REM connect_ex returns 0 when something is already listening on the port.
REM We exit 0 if the port is in use, 1 if free, so `if errorlevel 1` means free.
for /l %%i in (0,1,20) do (
    set /a candidate=%REQUESTED_PORT% + %%i
    python -c "import socket,sys; s=socket.socket(); rc=s.connect_ex(('127.0.0.1', int(sys.argv[1]))); s.close(); sys.exit(0 if rc==0 else 1)" !candidate! >nul 2>&1
    if errorlevel 1 (
        set PORT=!candidate!
        goto :port_found
    )
)
echo Could not find a free port in range %REQUESTED_PORT% to %REQUESTED_PORT%+20.
echo Set PORT=^<free-port^> and try again.
exit /b 1

:port_found
if not "%PORT%"=="%REQUESTED_PORT%" (
    echo Port %REQUESTED_PORT% was in use -- using %PORT% instead.
)

echo Starting server on http://localhost:%PORT%
echo.

REM ---- browser auto-open --------------------------------------------------
REM A short delay before launching; cmd's quoting makes a proper port-poll
REM loop fragile to embed inline, so we stick with a timeout here.
start "" /min cmd /c "timeout /t 3 >nul && start http://localhost:%PORT%"

python app.py
endlocal
