#!/usr/bin/env python3
"""
eSocial DAE Generator — Browserbase + Playwright
==================================================
Generates monthly eSocial DAE (Documento de Arrecadação do eSocial) for
domestic workers, using a Browserbase cloud browser so Fernanda can
authenticate via gov.br through the live view URL.

Flow:
  1. Create Browserbase session → get live view URL + connectUrl
  2. Navigate to eSocial login → click "Entrar com gov.br"
  3. Send live view URL to Fernanda → she logs into gov.br
  4. Poll until logged in (URL leaves sso.acesso.gov.br)
  5. Navigate to DAE generation → select competência → emit DAE
  6. Extract linha digitável / código de barras → send via Telegram

Usage:
  python3 esocial_dae.py                         # auto-detects previous month
  python3 esocial_dae.py --competencia 03/2026   # explicit month
  python3 esocial_dae.py --local                 # use local Chrome on :9222

Environment:
  BROWSERBASE_API_KEY    — Browserbase API key (required unless --local)
  BROWSERBASE_PROJECT_ID — Browserbase project ID (optional, auto-detected)
  TELEGRAM_BOT_TOKEN     — For notifications (falls back to OpenClaw config)
  TELEGRAM_CHAT_ID       — Chat to notify (default: 8409634074)
"""

import json
import os
import sys
import subprocess
import time
import traceback
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple
import argparse

# Install dependencies if missing
for pkg in ["requests", "playwright"]:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--break-system-packages", "-q"])

import requests
from playwright.sync_api import sync_playwright, Page, Browser

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_VERSION = "esocial-v1-2026-04-07"
print(f"[INIT] eSocial DAE Generator {SCRIPT_VERSION} at {datetime.now().isoformat()}")

ESOCIAL_LOGIN_URL = "https://login.esocial.gov.br/login.aspx"

TELEGRAM_CHAT_ID = "8409634074"

LOGIN_TIMEOUT = 300  # 5 minutes for Fernanda to log in
LOGIN_POLL_INTERVAL = 5  # Check every 5 seconds


# ============================================================================
# TELEGRAM
# ============================================================================

def _get_telegram_creds() -> Tuple[str, str]:
    """Get Telegram bot token and chat ID from env or OpenClaw config."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    if not token:
        config_paths = [
            "/data/.openclaw/openclaw.json",
            os.path.join(os.environ.get("OPENCLAW_STATE_DIR", ""), "openclaw.json"),
        ]
        for path in config_paths:
            try:
                with open(path, "r") as f:
                    config = json.load(f)
                channels = config.get("channels", {})
                for ch_name, ch_config in channels.items():
                    if isinstance(ch_config, dict):
                        t = ch_config.get("botToken") or ch_config.get("bot_token") or ch_config.get("token")
                        if t:
                            token = t
                            print(f"[TG] Found bot token in OpenClaw config ({path}, channel: {ch_name})")
                            break
                if token:
                    break
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                continue

    return token, chat_id


def send_telegram(message: str) -> None:
    """Send a message to Fernanda via Telegram."""
    token, chat_id = _get_telegram_creds()
    if not token or not chat_id:
        print(f"[TG] No Telegram credentials, skipping: {message}")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        print(f"[TG] Sent: {resp.status_code}")
    except Exception as e:
        print(f"[TG] Failed: {e}")


# ============================================================================
# BROWSERBASE
# ============================================================================

def create_browserbase_session() -> dict:
    """Create a Browserbase session. Returns dict with connectUrl, id, liveViewUrl."""
    api_key = os.environ.get("BROWSERBASE_API_KEY", "")
    if not api_key:
        raise RuntimeError("BROWSERBASE_API_KEY not set")

    project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")
    if not project_id:
        resp = requests.get(
            "https://api.browserbase.com/v1/projects",
            headers={"x-bb-api-key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        projects = resp.json()
        if not projects:
            raise RuntimeError("No Browserbase projects found")
        project_id = projects[0]["id"]
        print(f"[BB] Auto-detected project: {project_id}")

    resp = requests.post(
        "https://api.browserbase.com/v1/sessions",
        headers={"x-bb-api-key": api_key, "Content-Type": "application/json"},
        json={"projectId": project_id},
        timeout=15,
    )
    resp.raise_for_status()
    session = resp.json()
    session_id = session["id"]
    connect_url = session.get("connectUrl", "")
    print(f"[BB] Session created: {session_id}")

    debug_resp = requests.get(
        f"https://api.browserbase.com/v1/sessions/{session_id}/debug",
        headers={"x-bb-api-key": api_key},
        timeout=15,
    )
    live_view_url = ""
    if debug_resp.ok:
        debug_info = debug_resp.json()
        live_view_url = debug_info.get("debuggerFullscreenUrl", "") or debug_info.get("debuggerUrl", "")
        print(f"[BB] Live view URL: {live_view_url}")
    else:
        print(f"[BB] Could not get live view URL: {debug_resp.status_code}")

    return {
        "id": session_id,
        "connectUrl": connect_url,
        "liveViewUrl": live_view_url,
    }


# ============================================================================
# GOV.BR LOGIN HANDLING
# ============================================================================

def wait_for_govbr_login(page: Page, live_view_url: str) -> bool:
    """
    Wait for Fernanda to complete gov.br authentication via live view.
    Returns True if logged in, False if timed out.
    """
    send_telegram(
        f"eSocial DAE — preciso que você faça login no gov.br!\n\n"
        f"Abra o live view:\n{live_view_url}\n\n"
        f"Faça login com CPF + senha. Tenho 5 minutos."
    )

    start = time.time()
    while time.time() - start < LOGIN_TIMEOUT:
        current_url = page.url
        # Once logged in, URL will leave sso.acesso.gov.br
        if "sso.acesso.gov.br" not in current_url and "login.esocial" not in current_url:
            print(f"[LOGIN] Login detected! URL: {current_url}")
            send_telegram("Login no gov.br concluído! Continuando com a DAE...")
            return True
        # Also check if we're back on eSocial with a logged-in indicator
        try:
            # Look for any eSocial dashboard content that only shows when logged in
            if "esocial.gov.br" in current_url and "login" not in current_url:
                print(f"[LOGIN] Logged in — on eSocial dashboard: {current_url}")
                send_telegram("Login no gov.br concluído! Continuando com a DAE...")
                return True
        except Exception:
            pass
        time.sleep(LOGIN_POLL_INTERVAL)

    print("[LOGIN] Timed out waiting for gov.br login")
    send_telegram("Timeout esperando login no gov.br (5 min). Tente novamente.")
    return False


# ============================================================================
# DAE GENERATION
# ============================================================================

def generate_dae(page: Page, competencia: str, live_view_url: str) -> Optional[dict]:
    """
    Generate a DAE after login. Returns dict with linha_digitavel, valor, vencimento
    or None on failure.
    """
    print(f"\n[DAE] === Starting DAE generation for competência {competencia} ===")

    # Step 1: Navigate to eSocial login
    print("[DAE] Navigating to eSocial...")
    page.goto(ESOCIAL_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    page.screenshot(path="/tmp/esocial_01_login.png")

    # Step 2: Click "Entrar com gov.br"
    print("[DAE] Clicking 'Entrar com gov.br'...")
    try:
        page.get_by_text("Entrar com gov.br").click(timeout=10000)
        time.sleep(5)
        page.screenshot(path="/tmp/esocial_02_govbr.png")
        print(f"[DAE] On gov.br login page: {page.url}")
    except Exception as e:
        print(f"[DAE] Could not click gov.br login: {e}")
        page.screenshot(path="/tmp/esocial_err_govbr_click.png")
        return None

    # Step 3: Wait for Fernanda to log in
    print("[DAE] Waiting for gov.br authentication...")
    if not wait_for_govbr_login(page, live_view_url):
        return None

    time.sleep(3)
    page.screenshot(path="/tmp/esocial_03_logged_in.png")
    print(f"[DAE] Logged in. URL: {page.url}")

    # Step 4: Navigate to Empregador Doméstico module
    print("[DAE] Looking for Empregador Doméstico...")
    try:
        # The eSocial dashboard may show different modules
        # Try clicking "Empregador Doméstico" or the simplified module
        emp_domestico = page.get_by_text("Empregador Doméstico", exact=False)
        if emp_domestico.count() > 0:
            emp_domestico.first.click()
            time.sleep(3)
            print("[DAE] Clicked Empregador Doméstico")
        else:
            # May already be on the right page, or need "Módulo Simplificado"
            modulo = page.get_by_text("Módulo Simplificado", exact=False)
            if modulo.count() > 0:
                modulo.first.click()
                time.sleep(3)
                print("[DAE] Clicked Módulo Simplificado")
    except Exception as e:
        print(f"[DAE] Module navigation note: {e}")

    page.screenshot(path="/tmp/esocial_04_module.png")
    print(f"[DAE] Module page URL: {page.url}")

    # Step 5: Navigate to DAE generation
    # Look for "Emitir DAE", "Emitir Guia", "Folha/Recebimentos", etc.
    print("[DAE] Looking for DAE generation page...")
    dae_found = False
    dae_link_texts = [
        "Emitir DAE",
        "Emitir Guia",
        "DAE",
        "Folha de Pagamento",
        "Recebimentos e Pagamentos",
    ]
    for link_text in dae_link_texts:
        try:
            link = page.get_by_text(link_text, exact=False)
            if link.count() > 0 and link.first.is_visible():
                link.first.click()
                time.sleep(3)
                print(f"[DAE] Clicked: {link_text}")
                dae_found = True
                break
        except Exception:
            continue

    if not dae_found:
        print("[DAE] Could not find DAE link — dumping page for debugging")
        page.screenshot(path="/tmp/esocial_err_no_dae_link.png")
        # Try to get all visible links/buttons
        try:
            links = page.locator("a, button")
            for i in range(min(links.count(), 30)):
                txt = links.nth(i).text_content() or ""
                if txt.strip():
                    print(f"[DAE]   link/button: {txt.strip()[:80]}")
        except Exception:
            pass
        send_telegram(
            "Não encontrei o link para emitir DAE no eSocial. "
            "Verifique os screenshots em /tmp/esocial_*.png"
        )
        return None

    page.screenshot(path="/tmp/esocial_05_dae_page.png")
    print(f"[DAE] DAE page URL: {page.url}")

    # Step 6: Select competência
    print(f"[DAE] Looking for competência selector ({competencia})...")
    try:
        # eSocial typically has a month/year selector or a list of competências
        # Try different selector strategies
        comp_selected = False

        # Strategy 1: Look for a select/dropdown with month options
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            if sel.is_visible():
                options_text = sel.inner_text()
                if competencia in options_text or "Competência" in options_text:
                    sel.select_option(label=competencia)
                    comp_selected = True
                    print(f"[DAE] Selected competência via dropdown")
                    break

        # Strategy 2: Look for a clickable month in a list/table
        if not comp_selected:
            comp_link = page.get_by_text(competencia, exact=False)
            if comp_link.count() > 0:
                comp_link.first.click()
                comp_selected = True
                print(f"[DAE] Clicked competência text")

        # Strategy 3: Input field
        if not comp_selected:
            comp_input = page.locator("input[placeholder*='ompet'], input[placeholder*='mm'], input[name*='ompet']")
            if comp_input.count() > 0 and comp_input.first.is_visible():
                comp_input.first.fill(competencia)
                comp_selected = True
                print(f"[DAE] Filled competência input")

        if not comp_selected:
            print(f"[DAE] Could not find competência selector — continuing anyway")

        time.sleep(2)
    except Exception as e:
        print(f"[DAE] Competência selection error: {e}")

    page.screenshot(path="/tmp/esocial_06_competencia.png")

    # Step 7: Click to generate/emit DAE
    print("[DAE] Looking for emit/generate button...")
    emit_found = False
    emit_texts = [
        "Emitir DAE",
        "Emitir",
        "Gerar DAE",
        "Gerar Guia",
        "Emitir Guia",
        "Confirmar",
    ]
    for txt in emit_texts:
        try:
            btn = page.get_by_text(txt, exact=True)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                time.sleep(5)
                print(f"[DAE] Clicked: {txt}")
                emit_found = True
                break
        except Exception:
            continue

    if not emit_found:
        # Try any button with "emitir" or "gerar" in it
        try:
            btn = page.locator("button, a, input[type='button'], input[type='submit']")
            for i in range(min(btn.count(), 30)):
                txt = (btn.nth(i).text_content() or "").strip().lower()
                if any(kw in txt for kw in ["emitir", "gerar", "guia", "dae"]):
                    btn.nth(i).click()
                    time.sleep(5)
                    print(f"[DAE] Clicked button: {txt}")
                    emit_found = True
                    break
        except Exception:
            pass

    page.screenshot(path="/tmp/esocial_07_after_emit.png")

    if not emit_found:
        print("[DAE] Could not find emit button")
        send_telegram(
            "Não encontrei o botão para emitir DAE. "
            "Verifique os screenshots em /tmp/esocial_*.png"
        )
        return None

    # Step 8: Extract DAE info (linha digitável, valor, vencimento)
    print("[DAE] Extracting DAE info...")
    time.sleep(3)
    page.screenshot(path="/tmp/esocial_08_dae_result.png")

    result = extract_dae_info(page)

    if result:
        print(f"[DAE] DAE generated: {result}")
        page.screenshot(path="/tmp/esocial_09_done.png")
    else:
        print("[DAE] Could not extract DAE info")
        page.screenshot(path="/tmp/esocial_err_extract.png")
        send_telegram(
            "Não consegui extrair as informações da DAE. "
            "Verifique os screenshots em /tmp/esocial_*.png"
        )

    return result


def extract_dae_info(page: Page) -> Optional[dict]:
    """Extract linha digitável, valor, and vencimento from the DAE page."""
    # Try multiple methods to get page text (Angular/React apps can be tricky)
    page_text = ""
    try:
        page_text = page.inner_text("body") or ""
    except Exception:
        pass
    if len(page_text.strip()) < 20:
        try:
            page_text = page.text_content("body") or ""
        except Exception:
            pass
    if len(page_text.strip()) < 20:
        try:
            texts = page.locator("*").all_text_contents()
            page_text = " ".join(t.strip() for t in texts if t.strip())
        except Exception:
            pass

    print(f"[DAE] Extracted {len(page_text)} chars of page text")
    print(f"[DAE] Page text (first 1000 chars): {page_text[:1000]}")

    linha = None
    valor = None
    vencimento = None

    # DAE linha digitável / código de barras patterns
    linha_patterns = [
        r"(\d{11}-\d\s+\d{11}-\d\s+\d{11}-\d\s+\d{11}-\d)",  # GPS-like with dashes
        r"(\d{12}\s+\d{12}\s+\d{12}\s+\d{12})",  # 4x12 digits
        r"(\d{5}\.\d{5}\s+\d{5}\.\d{6}\s+\d{5}\.\d{6}\s+\d\s+\d{14})",  # Standard boleto
        r"[Ll]inha\s*[Dd]igit[áa]vel[:\s]*([0-9\s.\-]+)",  # After label
        r"[Cc][óo]digo\s*de\s*[Bb]arras[:\s]*([0-9\s.\-]+)",  # After "Código de Barras"
        r"(\d[\d\s.\-]{40,60}\d)",  # Any long digit string
    ]
    for pattern in linha_patterns:
        match = re.search(pattern, page_text, re.IGNORECASE)
        if match:
            linha = match.group(1).strip()
            break

    # If text extraction didn't find it, scan individual elements
    if not linha:
        try:
            all_elements = page.locator("span, div, p, td, input")
            for i in range(min(all_elements.count(), 150)):
                el_text = all_elements.nth(i).text_content() or ""
                # Also check input values (some portals put the code in an input)
                if not el_text.strip():
                    try:
                        el_text = all_elements.nth(i).input_value() or ""
                    except Exception:
                        pass
                for pattern in linha_patterns:
                    match = re.search(pattern, el_text)
                    if match:
                        linha = match.group(1).strip()
                        break
                if linha:
                    break
        except Exception as e:
            print(f"[DAE] Element scan error: {e}")

    # Extract valor
    valor_patterns = [
        r"Total[:\s]*R?\$?\s*([\d.,]+)",
        r"[Vv]alor[:\s]*R?\$?\s*([\d.,]+)",
        r"R\$\s*([\d.,]+)",
    ]
    for pattern in valor_patterns:
        match = re.search(pattern, page_text)
        if match:
            valor = match.group(1).strip()
            break

    # Extract vencimento
    venc_patterns = [
        r"[Vv]encimento[:\s]*(\d{2}/\d{2}/\d{4})",
        r"[Dd]ata\s*de\s*[Vv]encimento[:\s]*(\d{2}/\d{2}/\d{4})",
    ]
    for pattern in venc_patterns:
        match = re.search(pattern, page_text)
        if match:
            vencimento = match.group(1).strip()
            break

    if not linha:
        print("[DAE] Could not find linha digitável in page text")
        return None

    return {
        "linha_digitavel": linha,
        "valor": valor or "não encontrado",
        "vencimento": vencimento or "não encontrado",
    }


# ============================================================================
# MAIN
# ============================================================================

def get_competencia() -> str:
    """Get competência (previous month) in MM/YYYY format."""
    today = datetime.now()
    first_of_month = today.replace(day=1)
    prev_month = first_of_month - timedelta(days=1)
    return prev_month.strftime("%m/%Y")


def main():
    parser = argparse.ArgumentParser(description="eSocial DAE Generator")
    parser.add_argument("--competencia", type=str, help="Competência in MM/YYYY format (default: previous month)")
    parser.add_argument("--local", action="store_true", help="Use local Chrome (CDP on localhost:9222) instead of Browserbase")
    parser.add_argument("--cdp-url", type=str, default="http://localhost:9222", help="CDP URL for --local mode")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram notifications")
    args = parser.parse_args()

    competencia = args.competencia or get_competencia()
    print(f"[DAE] Competência: {competencia}")

    if args.no_telegram:
        global send_telegram
        _orig_send = send_telegram
        def send_telegram(msg):
            print(f"[TG-SKIP] {msg}")

    if args.local:
        connect_url = args.cdp_url
        live_view_url = "(local Chrome — visible on your desktop)"
        print(f"[DAE] Local mode: connecting to {connect_url}")
    else:
        try:
            session = create_browserbase_session()
        except Exception as e:
            msg = f"Falha ao criar sessão Browserbase: {e}"
            print(f"[DAE] {msg}")
            send_telegram(f"eSocial DAE falhou: {msg}")
            sys.exit(1)

        live_view_url = session["liveViewUrl"]
        connect_url = session["connectUrl"]

        if not connect_url:
            msg = "Browserbase session created but no connectUrl returned"
            print(f"[DAE] {msg}")
            send_telegram(f"eSocial DAE falhou: {msg}")
            sys.exit(1)

        send_telegram(
            f"eSocial DAE — competência *{competencia}*\n\n"
            f"Sessão Browserbase criada! Live view:\n{live_view_url}\n\n"
            f"Você vai precisar fazer login no gov.br quando eu pedir."
        )

    # Connect Playwright
    try:
        with sync_playwright() as pw:
            print(f"[DAE] Connecting to: {connect_url[:80]}...")
            browser = pw.chromium.connect_over_cdp(connect_url, timeout=30000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            result = generate_dae(page, competencia, live_view_url)

            if result:
                send_telegram(
                    f"✅ eSocial DAE — {competencia}\n"
                    f"Linha digitável: `{result['linha_digitavel']}`\n"
                    f"Valor: R$ {result['valor']}\n"
                    f"Vencimento: {result['vencimento']}"
                )
                print(f"\n[DAE] Done! DAE generated successfully.")
            else:
                send_telegram(
                    f"❌ eSocial DAE — {competencia}: "
                    f"Não consegui gerar a DAE. Verifique os screenshots."
                )
                print(f"\n[DAE] Failed — no DAE generated.")
                sys.exit(1)

            browser.close()

    except Exception as e:
        msg = f"Playwright connection failed: {e}"
        print(f"[DAE] {msg}")
        traceback.print_exc()
        send_telegram(f"eSocial DAE falhou: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
