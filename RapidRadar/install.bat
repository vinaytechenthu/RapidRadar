@echo off
setlocal EnableDelayedExpansion

echo.
echo =================================================================
echo   RapidRadar (Page Load Analysis + DB Scan) - Installer
echo =================================================================
echo.

:: ---------------------------------------------------------------
:: 1. Locate Python 3.x
:: ---------------------------------------------------------------
set PYTHON=

:: Try 'python' on PATH first
python --version >nul 2>&1
if %ERRORLEVEL% == 0 (
    for /f "tokens=2" %%V in ('python --version 2^>^&1') do set PYVER=%%V
    echo [INFO] Found Python !PYVER! on PATH
    set PYTHON=python
    goto :check_pip
)

:: Try 'py' launcher (Windows Python Launcher)
py --version >nul 2>&1
if %ERRORLEVEL% == 0 (
    for /f "tokens=2" %%V in ('py --version 2^>^&1') do set PYVER=%%V
    echo [INFO] Found Python !PYVER! via py launcher
    set PYTHON=py
    goto :check_pip
)

echo [ERROR] Python not found on this machine.
echo         Please install Python 3.9+ from https://www.python.org/downloads/
echo         Make sure to check "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:check_pip
:: ---------------------------------------------------------------
:: 2. Upgrade pip
:: ---------------------------------------------------------------
echo [INFO] Upgrading pip...
%PYTHON% -m pip install --upgrade pip --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] Could not upgrade pip - continuing with existing version.
)

:: ---------------------------------------------------------------
:: 3. Install Python packages
::    Priority: local wheels\ folder  ->  PyPI (online fallback)
:: ---------------------------------------------------------------
echo.
if exist "%~dp0wheels\" (
    echo [INFO] Installing packages from bundled wheels ^(offline^)...
    %PYTHON% -m pip install --no-index --find-links="%~dp0wheels" ^
        "greenlet>=3.1.1,<3.2" playwright
    if !ERRORLEVEL! NEQ 0 (
        echo [WARN] Offline install failed. Falling back to PyPI...
        goto :install_online
    )
    echo [OK]  Packages installed from bundled wheels.
    goto :install_flask
) else (
    echo [INFO] No bundled wheels found - installing from PyPI...
    goto :install_online
)

:install_online
%PYTHON% -m pip install --only-binary=:all: "greenlet>=3.1.1,<3.2"
%PYTHON% -m pip install --only-binary=:all: playwright
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Package installation failed.
    echo         Check your internet connection or ensure Visual Studio Build Tools
    echo         are installed if building from source is required.
    pause
    exit /b 1
)
echo [OK]  Packages installed from PyPI.

:install_flask
:: ---------------------------------------------------------------
:: 3.5 Install Flask for Web UI
:: ---------------------------------------------------------------
echo.
echo [INFO] Installing Flask for Web UI...
%PYTHON% -m pip install "flask>=2.3.0"
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] Flask installation failed - Web UI will not work.
    echo        You can still use the command-line interface.
) else (
    echo [OK]  Flask installed successfully.
)

:install_db_drivers
:: ---------------------------------------------------------------
:: 3.6 Install database drivers for DB Scan
::     SQL Server: pyodbc (+ "ODBC Driver 17/18 for SQL Server")
::     PostgreSQL: psycopg2-binary | Oracle: oracledb | MySQL: pymysql
:: ---------------------------------------------------------------
echo.
echo [INFO] Installing database drivers for DB Scan...
for %%P in ("pyodbc>=5.0.0" "psycopg2-binary>=2.9.6" "oracledb>=2.0.0" "pymysql>=1.1.0") do (
    %PYTHON% -m pip install --only-binary=:all: %%P --quiet
    if !ERRORLEVEL! NEQ 0 (
        echo [WARN] Could not install %%P - DB Scan for that database type will be unavailable.
    ) else (
        echo [OK]  %%P
    )
)

:playwright_browsers
:: ---------------------------------------------------------------
:: 4. Download Playwright browser binaries
:: ---------------------------------------------------------------
echo.
echo [INFO] Downloading Playwright browser binaries...
echo        (Chromium ~173 MB, Firefox ~110 MB, WebKit ~59 MB)
echo        This may take several minutes depending on your connection.
echo.
%PYTHON% -m playwright install
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Playwright browser download failed.
    echo         Check your internet connection and try again by running:
    echo           %PYTHON% -m playwright install
    pause
    exit /b 1
)

echo.
echo =================================================================
echo   Installation complete!
echo.
echo   Option 1 - Web Interface (Recommended):
echo     start_web_ui.bat
echo     Then open http://127.0.0.1:5000 in your browser
echo.
echo   Option 2 - Command Line:
echo     run.bat --login-url  https://yourapp.com/login
echo             --username   user@example.com
echo             --password   yourpassword
echo             --page-url   https://yourapp.com/dashboard
echo             --browser    chrome
echo.
echo   Or directly:
echo     %PYTHON% run_test.py --help
echo =================================================================
echo.
pause
endlocal
