@echo off
REM ============================================================
REM ChiralNet - Start Everything
REM Launches: Mosquitto (if not already running), the Python
REM backend, and the dashboard - then opens your browser.
REM Run this by double-clicking it from your ChiralNet folder.
REM ============================================================

cd /d "%~dp0"

echo ============================================
echo  ChiralNet - Starting all services
echo ============================================
echo.

REM ---- 1. Check Mosquitto service ----
echo [1/3] Checking Mosquitto broker...
sc query Mosquitto | find "RUNNING" >nul
if %errorlevel%==0 (
    echo       Mosquitto is already running.
) else (
    echo       Mosquitto is NOT running - attempting to start it...
    echo       ^(This needs admin rights. If it fails, open PowerShell
    echo        as Administrator and run: Start-Service -Name Mosquitto^)
    net start Mosquitto
)
echo.

REM ---- 2. Start the Python backend in its own window ----
echo [2/3] Starting backend (MQTT -^> SQLite)...
start "ChiralNet Backend" cmd /k python chiralnet_backend.py
echo       Backend window opened.
echo.

REM ---- 3. Start the dashboard in its own window ----
echo [3/3] Starting dashboard (Flask)...
start "ChiralNet Dashboard" cmd /k python chiralnet_dashboard.py
echo       Dashboard window opened.
echo.

REM ---- Wait a moment for Flask to boot, then open the browser ----
echo Waiting for dashboard to come online...
timeout /t 4 /nobreak >nul
start http://localhost:5000

echo.
echo ============================================
echo  All set. Two new windows are running:
echo    - ChiralNet Backend   (MQTT listener)
echo    - ChiralNet Dashboard (Flask web server)
echo  Close those windows (or Ctrl+C inside them)
echo  to stop everything. This window can be closed.
echo ============================================
pause
