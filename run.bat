@echo off
REM AUGUR -- start the app (Windows)
REM First-time install? Run setup.bat first.

setlocal
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

echo Starting server on http://localhost:%PORT%
echo.

REM Open browser after a brief delay
start "" /min cmd /c "timeout /t 2 >nul && start http://localhost:%PORT%"

python app.py
endlocal
