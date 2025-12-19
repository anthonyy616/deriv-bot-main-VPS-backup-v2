@echo off
REM ============================================
REM TRADING BOT - NSSM SERVICE INSTALLER
REM ============================================
REM Run this as Administrator on your VPS

SET SERVICE_NAME=TradingBotService

REM === IMPORTANT: Paths with spaces/special characters must be in quotes ===
SET "NSSM_PATH=C:\nssm\nssm.exe"
SET "PYTHON_PATH=C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe"
SET "PROJECT_PATH=C:\Users\Administrator\Downloads\trade-bot-deriv-v2"
SET "SCRIPT_PATH=%PROJECT_PATH%\run_forever.py"

echo ============================================
echo INSTALLING TRADING BOT SERVICE
echo ============================================
echo.
echo Service Name: %SERVICE_NAME%
echo Python Path:  %PYTHON_PATH%
echo Script Path:  %SCRIPT_PATH%
echo Project Path: %PROJECT_PATH%
echo.

REM Check if NSSM exists
if not exist "%NSSM_PATH%" (
    echo ERROR: NSSM not found at %NSSM_PATH%
    echo Please update the NSSM_PATH variable in this script.
    pause
    exit /b 1
)

REM Check if Python exists
if not exist "%PYTHON_PATH%" (
    echo ERROR: Python not found at %PYTHON_PATH%
    echo Please update the PYTHON_PATH variable in this script.
    pause
    exit /b 1
)

REM Check if project exists
if not exist "%PROJECT_PATH%" (
    echo ERROR: Project not found at %PROJECT_PATH%
    echo Please update the PROJECT_PATH variable in this script.
    pause
    exit /b 1
)

REM Create logs directory
if not exist "%PROJECT_PATH%\logs" (
    mkdir "%PROJECT_PATH%\logs"
    echo Created logs directory.
)

REM Remove existing service if present
echo Removing existing service (if any)...
"%NSSM_PATH%" stop %SERVICE_NAME% 2>nul
"%NSSM_PATH%" remove %SERVICE_NAME% confirm 2>nul

REM Install the service
echo Installing service...
"%NSSM_PATH%" install %SERVICE_NAME% "%PYTHON_PATH%" "%SCRIPT_PATH%"

REM Configure service parameters
echo Configuring service parameters...

REM Set working directory
"%NSSM_PATH%" set %SERVICE_NAME% AppDirectory "%PROJECT_PATH%"

REM Set restart behavior - restart on crash
"%NSSM_PATH%" set %SERVICE_NAME% AppExit Default Restart
"%NSSM_PATH%" set %SERVICE_NAME% AppRestartDelay 5000

REM Set throttle (time between restarts if it keeps crashing)
"%NSSM_PATH%" set %SERVICE_NAME% AppThrottle 10000

REM Set log file paths
"%NSSM_PATH%" set %SERVICE_NAME% AppStdout "%PROJECT_PATH%\logs\service_stdout.log"
"%NSSM_PATH%" set %SERVICE_NAME% AppStderr "%PROJECT_PATH%\logs\service_stderr.log"

REM Enable log file rotation (10 MB, rotate when size reached)
"%NSSM_PATH%" set %SERVICE_NAME% AppStdoutCreationDisposition 4
"%NSSM_PATH%" set %SERVICE_NAME% AppStderrCreationDisposition 4
"%NSSM_PATH%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM_PATH%" set %SERVICE_NAME% AppRotateBytes 10485760

REM Set description
"%NSSM_PATH%" set %SERVICE_NAME% Description "MT5 Ladder Grid Trading Bot - Production Service"

REM Set to auto-start on boot
"%NSSM_PATH%" set %SERVICE_NAME% Start SERVICE_AUTO_START

REM Set service account (LocalSystem has full access)
"%NSSM_PATH%" set %SERVICE_NAME% ObjectName LocalSystem

echo.
echo ============================================
echo SERVICE INSTALLED SUCCESSFULLY!
echo ============================================
echo.
echo To start the service:
echo   net start %SERVICE_NAME%
echo.
echo To stop the service:
echo   net stop %SERVICE_NAME%
echo.
echo To check service status:
echo   sc query %SERVICE_NAME%
echo.
echo Logs are stored in: %PROJECT_PATH%\logs\
echo.

REM Prompt to start service now
set /p START_NOW="Start the service now? (y/n): "
if /i "%START_NOW%"=="y" (
    echo Starting service...
    net start %SERVICE_NAME%
    echo.
    echo Service started! Check http://45.144.242.97:800/ in your browser.
)

pause
