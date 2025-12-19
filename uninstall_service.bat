@echo off
REM ============================================
REM TRADING BOT - NSSM SERVICE UNINSTALLER
REM ============================================
REM Run this as Administrator on your VPS

SET SERVICE_NAME=TradingBotService
SET "NSSM_PATH=C:\nssm\nssm.exe"

echo ============================================
echo UNINSTALLING TRADING BOT SERVICE
echo ============================================
echo.

REM Stop the service first
echo Stopping service...
net stop %SERVICE_NAME% 2>nul

REM Remove the service
echo Removing service...
"%NSSM_PATH%" remove %SERVICE_NAME% confirm

echo.
echo ============================================
echo SERVICE UNINSTALLED SUCCESSFULLY!
echo ============================================
echo.

pause
