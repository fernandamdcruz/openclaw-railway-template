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
  5. Navigate to Folha de Pagamento → select month → click "Emitir Guia"
  6. Download DAE PDF via HTTP with session cookies → parse with PyMuPDF
  7. Extract linha digitável, valor, vencimento → send via Telegram

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

_missing = []
for pkg in ["requests", "playwright", "fitz"]:
    try:
        __import__(pkg)
    except ImportError:
        _missing.append(pkg)
if _missing:
    print(f"[FATAL] Missing packages: {', '.join(_missing)}. Fix the Dockerfile.")
    sys.exit(1)

import fitz  # PyMuPDF
import requests
from playwright.sync_api import sync_playwright, Page, Browser

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_VERSION = "esocial-v2-2026-04-07"
print(f"[INIT] eSocial DAE Generator {SCRIPT_VERSION} at {datetime.now().isoformat()}")

ESOCIAL_LOGIN_URL = "https://login.esocial.gov.br/login.aspx"
ESOCIAL_FOLHA_URL = "https://www.esocial.gov.br/portal/FolhaPagamento/Listagem/ListarPagamentos"
ESOCIAL_EMIT_URL = "https://www.esocial.gov.br/portal/FolhaPagamento/EmitirGuia/EmitirGuiaMensal"

TELEGRAM_CHAT_ID = "8409634074"

LOGIN_TIMEOUT = 300  # 5 minutes for Fernanda to log in
LOGIN_POLL_INTERVAL = 5  # Check every 5 seconds

# Month name mapping for eSocial UI tabs
MONTH_NAMES = {
    "01": "Jan", "02": "Fev", "03": "Mar", "04": "Abr",
    "05": "Mai", "06": "Jun", "07": "Jul", "08": "Ago",
    "09": "Set", "10": "Out", "11": "Nov", "12": "Dez",
}


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

def autofill_govbr_credentials(page: Page) -> None:
    """
    Auto-fill CPF and password on the gov.br login page if credentials
    are available in env vars. This saves the user from typing on mobile.
    After filling, the bot verification challenge will still require
    human interaction via the live view.
    """
    cpf = os.environ.get("GOVBR_CPF", "")
    password = os.environ.get("GOVBR_PASSWORD", "")

    if not cpf or not password:
        print("[LOGIN] GOVBR_CPF/GOVBR_PASSWORD not set — user must enter credentials manually")
        return

    # Step 1: Fill CPF
    try:
        cpf_input = page.locator('input[placeholder*="CPF"], input#accountId, input[name*="cpf"]')
        if cpf_input.count() > 0 and cpf_input.first.is_visible(timeout=5000):
            cpf_input.first.fill(cpf)
            print(f"[LOGIN] CPF auto-filled")
            time.sleep(1)

            # Click Continuar
            continuar = page.get_by_text("Continuar", exact=True)
            if continuar.count() > 0:
                continuar.first.click()
                time.sleep(3)
                print("[LOGIN] Clicked Continuar")
            else:
                # Try submit button
                page.locator('input[type="submit"], button[type="submit"]').first.click()
                time.sleep(3)
        else:
            print("[LOGIN] CPF input not found on page")
            return
    except Exception as e:
        print(f"[LOGIN] CPF auto-fill error: {e}")
        return

    page.screenshot(path="/tmp/esocial_login_after_cpf.png")

    # Step 2: Fill password
    try:
        pwd_input = page.locator('input[type="password"]')
        if pwd_input.count() > 0 and pwd_input.first.is_visible(timeout=5000):
            pwd_input.first.fill(password)
            print("[LOGIN] Password auto-filled")
            time.sleep(1)

            # Click Entrar
            entrar = page.get_by_text("Entrar", exact=True)
            if entrar.count() > 0:
                entrar.first.click()
                time.sleep(5)
                print("[LOGIN] Clicked Entrar")
            else:
                page.locator('input[type="submit"], button[type="submit"]').first.click()
                time.sleep(5)
        else:
            print("[LOGIN] Password input not found — page may have changed")
    except Exception as e:
        print(f"[LOGIN] Password auto-fill error: {e}")

    page.screenshot(path="/tmp/esocial_login_after_password.png")
    print(f"[LOGIN] After credential submission, URL: {page.url}")


def wait_for_govbr_login(page: Page, live_view_url: str) -> bool:
    """
    Wait for Fernanda to complete gov.br authentication via live view.
    Auto-fills CPF + password if env vars are set, then waits for
    human verification challenge if needed.
    Returns True if logged in, False if timed out.
    """
    # Try auto-filling credentials first
    if "sso.acesso.gov.br" in page.url:
        autofill_govbr_credentials(page)

    # Check if already through (auto-fill + no verification = instant login)
    current_url = page.url
    if "sso.acesso.gov.br" not in current_url and "login.esocial" not in current_url:
        print(f"[LOGIN] Login completed after auto-fill! URL: {current_url}")
        send_telegram("Login no gov.br concluído automaticamente! Continuando com a DAE...")
        return True

    # Still on gov.br — verification challenge likely appeared
    # Notify Fernanda to handle it via live view
    send_telegram(
        f"eSocial DAE — CPF e senha já preenchidos!\n\n"
        f"Falta só a verificação de segurança.\n"
        f"Abra o live view:\n{live_view_url}\n\n"
        f"Complete a verificação. Tenho 5 minutos."
    )

    start = time.time()
    while time.time() - start < LOGIN_TIMEOUT:
        current_url = page.url
        if "sso.acesso.gov.br" not in current_url and "login.esocial" not in current_url:
            print(f"[LOGIN] Login detected! URL: {current_url}")
            send_telegram("Login no gov.br concluído! Continuando com a DAE...")
            return True
        time.sleep(LOGIN_POLL_INTERVAL)

    print("[LOGIN] Timed out waiting for gov.br login")
    send_telegram("Timeout esperando login no gov.br (5 min). Tente novamente.")
    return False


# ============================================================================
# DAE GENERATION
# ============================================================================

def generate_dae(page: Page, competencia: str, live_view_url: str) -> Optional[dict]:
    """
    Generate a DAE for the given competência.
    Returns dict with linha_digitavel, valor, vencimento or None on failure.
    """
    # Parse competencia MM/YYYY
    parts = competencia.split("/")
    if len(parts) != 2:
        print(f"[DAE] Invalid competência format: {competencia}")
        return None
    month_str, year_str = parts[0], parts[1]
    comp_yyyymm = f"{year_str}{month_str}"  # e.g. "202603"
    month_tab = MONTH_NAMES.get(month_str, "")

    print(f"\n[DAE] === Starting DAE generation for {competencia} (tab: {month_tab}, code: {comp_yyyymm}) ===")

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
        # Maybe already logged in (session still valid)
        if "esocial.gov.br" in page.url and "login" not in page.url:
            print(f"[DAE] Already logged in! URL: {page.url}")
        else:
            print(f"[DAE] Could not click gov.br login: {e}")
            page.screenshot(path="/tmp/esocial_err_govbr.png")
            return None

    # Step 3: Wait for login (or skip if already logged in)
    if "sso.acesso.gov.br" in page.url or "login.esocial" in page.url:
        print("[DAE] Waiting for gov.br authentication...")
        if not wait_for_govbr_login(page, live_view_url):
            return None

    time.sleep(3)
    page.screenshot(path="/tmp/esocial_03_logged_in.png")
    print(f"[DAE] Logged in. URL: {page.url}")

    # Step 4: Navigate to Folha de Pagamento page
    # After login, we land on the eSocial dashboard (Empregador Doméstico)
    # Go directly to the Folha de Pagamento listing
    print("[DAE] Navigating to Folha de Pagamento...")
    page.goto(ESOCIAL_FOLHA_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    page.screenshot(path="/tmp/esocial_04_folha.png")
    print(f"[DAE] Folha page URL: {page.url}")

    # Dismiss "Atenção" modal that appears on first load
    # ("Certifique-se de que você realizou o lançamento de todos os eventos do mês...")
    # The modal's jQuery UI overlay (.ui-widget-overlay) blocks all clicks until dismissed.
    print("[DAE] Checking for 'Atenção' modal...")
    try:
        ok_btn = page.locator(".ui-dialog button:has-text('Ok'), .ui-dialog-buttonset button:has-text('Ok')").first
        if ok_btn.is_visible(timeout=3000):
            ok_btn.click()
            print("[DAE] Dismissed 'Atenção' modal")
            # Wait for the overlay to fully disappear
            page.locator(".ui-widget-overlay").wait_for(state="hidden", timeout=5000)
            time.sleep(1)
    except Exception as e:
        print(f"[DAE] No 'Atenção' modal (or already dismissed): {e}")

    # Step 5: Select year tab if needed
    print(f"[DAE] Selecting year {year_str}...")
    try:
        year_tab = page.get_by_text(year_str, exact=True)
        if year_tab.count() > 0 and year_tab.first.is_visible():
            year_tab.first.click()
            time.sleep(2)
            print(f"[DAE] Year {year_str} selected")
    except Exception as e:
        print(f"[DAE] Year tab note: {e}")

    # Step 6: Select month tab
    print(f"[DAE] Selecting month {month_tab}...")
    try:
        month_btn = page.get_by_text(month_tab, exact=True)
        if month_btn.count() > 0 and month_btn.first.is_visible():
            month_btn.first.click()
            time.sleep(2)
            print(f"[DAE] Month {month_tab} selected")
    except Exception as e:
        print(f"[DAE] Month tab note: {e}")

    page.screenshot(path="/tmp/esocial_05_month_selected.png")

    # Step 7: Click "Emitir Guia" button
    print("[DAE] Clicking 'Emitir Guia'...")
    try:
        emitir = page.get_by_text("Emitir Guia", exact=True)
        emitir.click(timeout=5000)
        time.sleep(5)
        print(f"[DAE] Emitir Guia clicked — PDF should be loading")
    except Exception as e:
        print(f"[DAE] Emitir Guia error: {e}")
        page.screenshot(path="/tmp/esocial_err_emitir.png")
        # Try the direct URL as fallback
        print(f"[DAE] Trying direct URL: {ESOCIAL_EMIT_URL}?competencia={comp_yyyymm}")
        page.goto(f"{ESOCIAL_EMIT_URL}?competencia={comp_yyyymm}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)

    page.screenshot(path="/tmp/esocial_06_dae_pdf.png")
    print(f"[DAE] DAE page URL: {page.url}")

    # Step 8: Download the DAE PDF and extract info
    # The DAE is rendered as a PDF in Chrome's built-in viewer.
    # Chrome's PDF viewer doesn't expose text to Playwright.
    # We download the PDF via HTTP using the browser's session cookies,
    # then parse it with PyMuPDF to extract the linha digitável.
    print("[DAE] Downloading DAE PDF via HTTP...")
    result = download_and_parse_dae_pdf(page, comp_yyyymm)

    if result:
        print(f"[DAE] DAE extracted: {result}")
        page.screenshot(path="/tmp/esocial_07_done.png")
    else:
        print("[DAE] Could not extract DAE info from PDF")
        page.screenshot(path="/tmp/esocial_err_extract.png")
        send_telegram(
            "Não consegui extrair as informações da DAE. "
            "Verifique os screenshots em /tmp/esocial_*.png"
        )

    return result


def download_and_parse_dae_pdf(page: Page, comp_yyyymm: str) -> Optional[dict]:
    """
    Download the DAE PDF using the browser's cookies and parse it with PyMuPDF.
    The PDF URL is: EmitirGuiaMensal?competencia=YYYYMM
    """
    # Get cookies from the browser via CDP
    try:
        client = page.context.new_cdp_session(page)
        result = client.send("Network.getCookies", {"urls": ["https://www.esocial.gov.br"]})
        cookies = result.get("cookies", [])
        cookie_header = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies)
        print(f"[DAE] Got {len(cookies)} cookies from browser")
    except Exception as e:
        print(f"[DAE] Could not get cookies via CDP: {e}")
        # Fallback: get cookies from Playwright context
        try:
            cookies = page.context.cookies()
            cookie_header = "; ".join(
                f'{c["name"]}={c["value"]}' for c in cookies
                if "esocial" in c.get("domain", "")
            )
            print(f"[DAE] Got cookies via Playwright context")
        except Exception as e2:
            print(f"[DAE] Could not get cookies at all: {e2}")
            return None

    # Download the PDF
    pdf_url = f"{ESOCIAL_EMIT_URL}?competencia={comp_yyyymm}"
    print(f"[DAE] Downloading: {pdf_url}")
    try:
        resp = requests.get(
            pdf_url,
            headers={
                "Cookie": cookie_header,
                "Accept": "application/pdf",
            },
            timeout=30,
        )
        ct = resp.headers.get("content-type", "")
        print(f"[DAE] Response: {resp.status_code}, Content-Type: {ct}, Size: {len(resp.content)}")

        if resp.status_code != 200:
            print(f"[DAE] HTTP error downloading PDF")
            return None

        if not ("pdf" in ct.lower() or resp.content[:4] == b"%PDF"):
            print(f"[DAE] Response is not a PDF: {resp.content[:200]}")
            return None

    except Exception as e:
        print(f"[DAE] PDF download error: {e}")
        return None

    # Save PDF locally
    pdf_path = f"/tmp/esocial_dae_{comp_yyyymm}.pdf"
    with open(pdf_path, "wb") as f:
        f.write(resp.content)
    print(f"[DAE] PDF saved: {pdf_path} ({len(resp.content)} bytes)")

    # Parse with PyMuPDF
    return extract_dae_from_pdf(pdf_path)


def extract_dae_from_pdf(pdf_path: str) -> Optional[dict]:
    """Extract linha digitável, valor, and vencimento from a DAE PDF file."""
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        for pg in doc:
            full_text += pg.get_text() + "\n"
        doc.close()
    except Exception as e:
        print(f"[DAE] PDF parse error: {e}")
        return None

    print(f"[DAE] PDF text ({len(full_text)} chars):")
    print(full_text[:1500])

    linha = None
    valor = None
    vencimento = None

    # DAE linha digitável: "85850000015 0 49270432261 4 10071626097 9 32317340917 0"
    # Pattern: 4 groups of (11 digits + space + 1 digit)
    linha_patterns = [
        r"(\d{11}\s+\d\s+\d{11}\s+\d\s+\d{11}\s+\d\s+\d{11}\s+\d)",  # DAE format (space-separated)
        r"(\d{11}-\d\s+\d{11}-\d\s+\d{11}-\d\s+\d{11}-\d)",  # With dashes
        r"(\d{12}\s+\d{12}\s+\d{12}\s+\d{12})",  # No separators within groups
    ]
    for pattern in linha_patterns:
        match = re.search(pattern, full_text)
        if match:
            linha = match.group(1).strip()
            # Normalize: collapse multiple spaces, keep the groups clear
            linha = re.sub(r"\s+", " ", linha)
            break

    # Extract valor from "Valor Total do Documento\n1.549,27" or "Valor:\n1.549,27"
    valor_patterns = [
        r"Valor\s*Total[^0-9]*([\d.,]+)",
        r"Valor[:\s]*([\d.,]+)",
        r"Totais\s*([\d.,]+)",
    ]
    for pattern in valor_patterns:
        match = re.search(pattern, full_text)
        if match:
            valor = match.group(1).strip()
            break

    # Extract vencimento from "Data de Vencimento\n20/04/2026" or "Pagar até:\n20/04/2026"
    venc_patterns = [
        r"Vencimento\s*(\d{2}/\d{2}/\d{4})",
        r"Pagar\s*(?:este documento )?at[ée][:\s]*(\d{2}/\d{2}/\d{4})",
    ]
    for pattern in venc_patterns:
        match = re.search(pattern, full_text)
        if match:
            vencimento = match.group(1).strip()
            break

    if not linha:
        print("[DAE] Could not find linha digitável in PDF text")
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
