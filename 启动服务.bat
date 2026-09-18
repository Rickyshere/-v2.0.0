@echo off
title Railway Planner - One Click Start
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo    Railway Transit Planner - Auto Setup
echo ============================================
echo.

REM ===== 1. Detect Python =====
echo [1/4] Checking Python...
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    where py >nul 2>&1 && set "PY=py"
)
if defined PY goto py_ok
echo.
echo   [ERROR] Python not found.
echo   Please install Python 3.9 or newer.
echo   Download page:  https://www.python.org/downloads/
echo   Important: when installing, tick "Add Python to PATH".
echo.
pause
exit /b 1
:py_ok
%PY% --version >nul 2>&1 && goto ver_ok
echo.
echo   [ERROR] Python exists but cannot run.
echo   Please reinstall Python and tick "Add Python to PATH".
echo.
pause
exit /b 1
:ver_ok
%PY% --version
echo.

REM ===== 2. Install dependencies =====
echo [2/4] Checking dependencies...
%PY% -c "import fastapi, uvicorn, networkx, pandas, requests" >nul 2>&1
if errorlevel 1 (
    echo       First run: installing dependencies, please wait...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [ERROR] Failed to install dependencies.
        echo   Please check network, then run manually:
        echo   pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo       Dependencies installed.
) else (
    echo       Dependencies ready.
)
echo.

REM ===== 3. Generate data if missing =====
echo [3/4] Checking data files...
if not exist stations.csv (
    echo       stations.csv missing, generating...
    %PY% upgrade_data.py
    if errorlevel 1 goto data_err
)
if not exist lines.csv (
    echo       lines.csv missing, generating...
    %PY% upgrade_data.py
    if errorlevel 1 goto data_err
)
echo       Data ready.
echo.
goto check_port
:data_err
echo   [ERROR] Failed to generate data.
echo   Please check the file main_railway_line.txt.
pause
exit /b 1

REM ===== 4. Start server =====
:check_port
echo [4/4] Starting server...
echo       URL: http://127.0.0.1:8000

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 goto not_busy
echo.
echo   [NOTE] Port 8000 already in use.
echo   A server may already be running.
echo   Opening browser...
start "" "http://127.0.0.1:8000"
echo   If page fails, stop other programs using port 8000.
pause
exit /b 1

:not_busy
if exist station_codes.csv (
    echo       Live train-query via 12306 enabled.
)
echo.

start "RailwayPlanner-Server" /min cmd /c "%PY% main.py"

REM Wait for the server port to be ready, up to ~20 sec
set /a attempts=0
:waitloop
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto ready
set /a attempts+=1
if !attempts! lss 20 goto waitloop

echo   [NOTE] Server is taking a while to start.
echo   If it does not open, go to  http://127.0.0.1:8000
echo.
goto done

:ready
start "" "http://127.0.0.1:8000"
echo   Server is ready.

:done
echo.
echo ============================================
echo    Server is running, browser should open.
echo    To STOP: close the "RailwayPlanner-Server" window.
echo.
echo    LAN access: allow port 8000 in firewall,
echo    others can visit  http://THIS-PC-IP:8000
echo ============================================
echo.
pause
