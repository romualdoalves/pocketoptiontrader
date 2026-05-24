@echo off
chcp 65001 >nul 2>&1
title AlpacaTrader — Live Logs
cd /d "%~dp0"

echo Escolha qual servico monitorar:
echo.
echo   [1] freqtrade  (bot principal)
echo   [2] grafana
echo   [3] todos (interleaved)
echo.
set /p "CHOICE=Digite 1, 2 ou 3: "

if "%CHOICE%"=="1" (
    docker compose -f docker-compose.yml -f docker-compose.windows-test.yml logs -f --tail=50 freqtrade
) else if "%CHOICE%"=="2" (
    docker compose -f docker-compose.yml -f docker-compose.windows-test.yml logs -f --tail=50 grafana
) else (
    docker compose -f docker-compose.yml -f docker-compose.windows-test.yml logs -f --tail=50
)
pause
