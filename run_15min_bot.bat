@echo off
cd /d "E:\Dell Inspiron\P\Claude Code\AlpacaTrader"
python src\intraday_trader.py --ticker BTC/USD >> logs\intraday_trader.log 2>&1
