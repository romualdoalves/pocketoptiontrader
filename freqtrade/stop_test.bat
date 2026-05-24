@echo off
chcp 65001 >nul 2>&1
title AlpacaTrader — Stop
cd /d "%~dp0"
echo Parando AlpacaTrader stack...
docker compose -f docker-compose.yml -f docker-compose.windows-test.yml down
echo.
echo Stack parado. Os dados do SQLite e logs foram preservados em user_data\
pause
