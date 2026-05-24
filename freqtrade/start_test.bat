@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
title AlpacaTrader — Windows Test Deploy

echo.
echo ============================================================
echo   AlpacaTrader - Windows Test Deploy
echo   Freqtrade + Grafana (sem GCP, dry_run=true)
echo ============================================================
echo.

:: ── 0. Check working directory ───────────────────────────────────────────────
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
echo [INFO] Diretorio: %SCRIPT_DIR%

:: ── 1. Check Docker Desktop ───────────────────────────────────────────────────
echo.
echo [1/6] Verificando Docker Desktop...
docker info >nul 2>&1
if errorlevel 1 (
    echo [AVISO] Docker nao esta rodando. Iniciando Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" 2>nul
    if errorlevel 1 (
        start "" "%LOCALAPPDATA%\Docker\Docker Desktop.exe" 2>nul
    )
    echo [INFO] Aguardando Docker iniciar (pode levar 30-60 segundos)...
    :wait_docker
    timeout /t 5 /nobreak >nul
    docker info >nul 2>&1
    if errorlevel 1 (
        set /p _dummy="   Ainda aguardando... (pressione ENTER para checar manualmente) "
        goto wait_docker
    )
    echo [OK] Docker Desktop iniciado!
) else (
    echo [OK] Docker Desktop esta rodando.
)

:: ── 2. Build XAI image ───────────────────────────────────────────────────────
echo.
echo [2/6] Construindo imagem freqtrade-xai:local...
echo      (primeira vez demora ~3 min para baixar dependencias)
docker build -f Dockerfile.xai -t freqtrade-xai:local . 2>&1
if errorlevel 1 (
    echo [ERRO] Falha ao construir imagem. Veja os logs acima.
    pause
    exit /b 1
)
echo [OK] Imagem construida com sucesso.

:: ── 3. Create .env from root project .env ────────────────────────────────────
echo.
echo [3/6] Configurando variaveis de ambiente...

if exist ".env" (
    echo [OK] .env ja existe — mantendo configuracao atual.
    goto env_done
)

:: Read Alpaca keys from parent .env
set "ROOT_ENV=%SCRIPT_DIR%..\..env"
if not exist "%ROOT_ENV%" (
    set "ROOT_ENV=%SCRIPT_DIR%..\.env"
)

set "ALPACA_KEY="
set "ALPACA_SECRET="

if exist "%ROOT_ENV%" (
    for /f "usebackq tokens=1,2 delims==" %%A in ("%ROOT_ENV%") do (
        if "%%A"=="ALPACA_API_KEY"    set "ALPACA_KEY=%%B"
        if "%%A"=="ALPACA_API_SECRET" set "ALPACA_SECRET=%%B"
    )
)

if "!ALPACA_KEY!"=="" (
    echo.
    echo [ATENCAO] Chave Alpaca nao encontrada no .env raiz.
    set /p "ALPACA_KEY=Cole sua ALPACA_API_KEY: "
    set /p "ALPACA_SECRET=Cole sua ALPACA_API_SECRET: "
)

:: Generate random JWT secret using Python
for /f "delims=" %%J in ('python -c "import secrets; print(secrets.token_hex(32))"') do set "JWT_SECRET=%%J"

:: Write freqtrade/.env
(
echo # Gerado automaticamente por start_test.bat
echo ALPACA_API_KEY=!ALPACA_KEY!
echo ALPACA_API_SECRET=!ALPACA_SECRET!
echo.
echo FREQTRADE_JWT_SECRET=!JWT_SECRET!
echo FREQTRADE_API_PASSWORD=alpaca2025
echo.
echo GCP_PROJECT_ID=test-project
echo BQ_DATASET=alpacatrader
echo GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp_service_account.json
echo.
echo GRAFANA_ADMIN_USER=admin
echo GRAFANA_ADMIN_PASSWORD=admin123
echo.
echo TELEGRAM_BOT_TOKEN=
echo TELEGRAM_CHAT_ID=
) > .env

echo [OK] .env criado com suas chaves Alpaca.

:env_done

:: ── 4. Create required directories ───────────────────────────────────────────
echo.
echo [4/6] Criando diretorios necessarios...
if not exist "user_data\logs"          mkdir "user_data\logs"
if not exist "user_data\narratives"    mkdir "user_data\narratives"
if not exist "user_data\visual_proofs" mkdir "user_data\visual_proofs"
if not exist "user_data\data"          mkdir "user_data\data"
echo [OK] Diretorios criados.

:: ── 5. Stop any previous containers ──────────────────────────────────────────
echo.
echo [5/6] Parando containers antigos (se houver)...
docker compose -f docker-compose.yml -f docker-compose.windows-test.yml down --remove-orphans >nul 2>&1
echo [OK] Containers anteriores parados.

:: ── 6. Start the stack ───────────────────────────────────────────────────────
echo.
echo [6/6] Iniciando AlpacaTrader stack...
docker compose -f docker-compose.yml -f docker-compose.windows-test.yml up -d
if errorlevel 1 (
    echo.
    echo [ERRO] Falha ao iniciar containers.
    echo Executando diagnostico...
    docker compose -f docker-compose.yml -f docker-compose.windows-test.yml logs --tail=30
    pause
    exit /b 1
)

:: ── Done ─────────────────────────────────────────────────────────────────────
echo.
echo ============================================================
echo   DEPLOY CONCLUIDO COM SUCESSO!
echo ============================================================
echo.
echo   Freqtrade UI  : http://localhost:8888
echo   Grafana        : http://localhost:3000
echo.
echo   Credenciais Grafana : admin / admin123
echo   Credenciais API     : freqtrade / alpaca2025
echo.
echo   (Porta 8080 reservada pelo IIS do Windows -- usando 8888)
echo.
echo   Para ver logs em tempo real:
echo     docker compose logs -f freqtrade
echo.
echo   Para parar tudo:
echo     docker compose -f docker-compose.yml -f docker-compose.windows-test.yml down
echo.
echo ============================================================

:: Open browser after 5 seconds
echo Abrindo Freqtrade UI em 5 segundos...
timeout /t 5 /nobreak >nul
start "" "http://localhost:8888"
timeout /t 2 /nobreak >nul
start "" "http://localhost:3000"

echo.
pause
