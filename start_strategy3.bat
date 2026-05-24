@echo off
title Strategy 3 — AI-Optimized Defensive Allocator (LIVE)
cd /d "E:\Dell Inspiron\P\Claude Code\AlpacaTrader"

:: Load .env if it exists
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" set "%%A=%%B"
    )
)

:: Create logs dir if needed
if not exist "logs" mkdir logs

:: Set live mode (paper account)
set STRATEGY3_LIVE=true

:: Open a second terminal tailing the log in real-time
start "Strategy3 LOG" powershell -Command "Get-Content logs\strategy3.log -Wait -Tail 50"

:: Run the strategy (Python writes to logs\strategy3.log via FileHandler)
python -m src.strategy3.strategy3_main
pause
