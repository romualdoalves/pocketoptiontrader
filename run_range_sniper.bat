@echo off
title PocketOption Range Sniper
echo =============================================
echo  PocketOption Range Sniper Bot
echo  Conta: %POCKET_DEMO% (1=demo, 0=real)
echo =============================================
echo.

:: Inicia bot runner em uma janela
start "Bot Runner" cmd /k "python -m src.bot_runner"

:: Inicia UI Streamlit em outra janela
start "Streamlit UI" cmd /k "streamlit run app_streamlit.py --server.port 8501"

:: Abre o navegador após 3 segundos
timeout /t 3 /nobreak >nul
start http://localhost:8501

echo Bots iniciados. Acesse: http://localhost:8501
pause
