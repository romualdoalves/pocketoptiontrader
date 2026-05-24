"""
Captura as credenciais de sessão do PocketOption abrindo um browser real.

COMO USAR (no VPS):
  1. pip install playwright && playwright install chromium
  2. Instalar display virtual: apt-get install -y xvfb x11-utils
  3. Rodar: xvfb-run -a python scripts/capture_session.py
     OU (com X11 forwarding via SSH -X):
         DISPLAY=:0 python scripts/capture_session.py

  O script vai abrir o PocketOption no browser, você faz login
  (Google Account), e assim que a sessão WebSocket é estabelecida
  o script salva as credenciais no .env automaticamente.

COMO USAR (no Windows, sua máquina local):
  1. pip install playwright && playwright install chromium
  2. python scripts/capture_session.py
  Abre browser normalmente — mesmo IP da máquina local.
"""
import asyncio
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

try:
    from playwright.async_api import async_playwright, Route, WebSocket
except ImportError:
    print("Playwright não instalado. Rode: pip install playwright && playwright install chromium")
    sys.exit(1)

ENV_PATH = Path(__file__).parent.parent / ".env"

TARGET_URL = "https://pocketoption.com/cabinet/demo-quick-high-low"

CAPTURED: dict = {}


def _update_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"  .env atualizado: {key}='{value[:8]}...'")


async def main() -> None:
    async with async_playwright() as pw:
        # headless=False para que o Google OAuth funcione sem bloqueio
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        print("=" * 60)
        print("CAPTURA DE CREDENCIAIS POCKETOPTION")
        print("=" * 60)
        print(f"Abrindo {TARGET_URL}")
        print("Faça login com sua Google Account.")
        print("Aguardando conexão WebSocket com auth...")
        print("=" * 60)

        page = await context.new_page()

        # Intercepta tráfego WebSocket para capturar o auth
        async def on_websocket(ws: WebSocket) -> None:
            if "socket.io" not in ws.url:
                return

            async def on_message_sent(payload: str) -> None:
                if not payload.startswith("42"):
                    return
                try:
                    data = json.loads(payload[2:])
                except Exception:
                    return
                if not isinstance(data, list) or data[0] != "auth":
                    return

                auth_data = data[1] if len(data) > 1 else {}
                session = auth_data.get("session") or auth_data.get("sessionToken", "")
                uid = auth_data.get("uid", "")
                is_demo = auth_data.get("isDemo", 1)

                if not session or not uid:
                    return

                CAPTURED["session"] = str(session)
                CAPTURED["uid"] = str(uid)
                CAPTURED["is_demo"] = str(is_demo)

                print(f"\n✓ Auth capturado!")
                print(f"  uid     = {uid}")
                print(f"  session = {session[:8]}...{session[-4:]}")
                print(f"  isDemo  = {is_demo}")

            ws.on("framesent", lambda frame: asyncio.create_task(
                on_message_sent(frame.payload) if isinstance(frame.payload, str) else asyncio.sleep(0)
            ))

        page.on("websocket", on_websocket)

        await page.goto(TARGET_URL)

        # Aguarda até 5 minutos para o usuário fazer login e o auth ser capturado
        for _ in range(300):
            if CAPTURED:
                break
            await asyncio.sleep(1)

        if not CAPTURED:
            print("\n✗ Timeout: auth não capturado em 5 minutos.")
            await browser.close()
            return

        # Captura também o ci_session cookie
        cookies = await context.cookies()
        ci_session = next(
            (c["value"] for c in cookies if c["name"] == "ci_session"), ""
        )

        print("\nSalvando no .env...")
        _update_env("POCKET_SECRET", CAPTURED["session"])
        _update_env("POCKET_UID", CAPTURED["uid"])
        _update_env("POCKET_DEMO", CAPTURED["is_demo"])
        if ci_session:
            _update_env("POCKET_SSID", ci_session)
            print(f"  .env atualizado: POCKET_SSID='{ci_session[:16]}...'")

        print("\n✓ .env atualizado com sucesso!")
        print("Reinicie o bot: docker compose up -d --force-recreate bot")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
