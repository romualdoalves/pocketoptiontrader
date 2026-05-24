@echo off
title AlpacaTrader - 5min BTC/USD Bot
cd /d "E:\Dell Inspiron\P\Claude Code\AlpacaTrader"
start "AlpacaTrader LOG" powershell -Command "Get-Content logs\scheduler.log -Wait -Tail 30"
python scheduler.py --ticker BTC/USD
pause
