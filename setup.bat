@echo off
REM AUGUR -- one-time install (Windows)
REM Creates a Python virtual environment, installs dependencies, and
REM initializes the local SQLite database.

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ====================================================
echo   AUGUR -- Setup
echo ====================================================
echo.

REM ---- Python check -------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found.
    echo.
    echo Install Python 3.9+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VERSION=%%v
echo Python %PY_VERSION% detected

REM ---- Virtual environment ------------------------------------------------
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        echo You may need to install python3-venv.
        exit /b 1
    )
)

call venv\Scripts\activate.bat

REM ---- Dependencies -------------------------------------------------------
echo Upgrading pip...
python -m pip install -q --upgrade pip

echo Installing dependencies (this may take a few minutes)...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    exit /b 1
)

REM ---- Local config -------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        copy /Y .env.example .env >nul
        echo Created .env from .env.example
    )
)

REM ---- Initialize the database --------------------------------------------
echo Initializing local database...
python -c "import database; database.init_db(); print('wealth.db ready')"

echo.
echo ====================================================
echo   Setup complete. Run run.bat to start the app.
echo ====================================================
echo.
endlocal
