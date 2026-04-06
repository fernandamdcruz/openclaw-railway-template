#!/usr/bin/env python3
"""
GPS Boleto Generator — Browserbase + Playwright
=================================================
Generates monthly GPS (Guia da Previdência Social) boletos for Fernanda and Max
via the SAL portal (sal.rfb.gov.br), using a Browserbase cloud browser so Fernanda
can solve CAPTCHAs via the live view URL.

Flow:
  1. Create Browserbase session → get live view URL + connectUrl
  2. Send live view URL to Fernanda via Telegram
  3. Connect Playwright to the Browserbase session
  4. For each person (Fernanda, Max):
     a. Navigate to SAL portal
     b. Fill NIT + category
     c. Pause at CAPTCHA → notify Fernanda, poll until solved
     d. Click Consultar → select payment code → set competência
     e. Generate boleto → extract linha digitável
     f. Send result via Telegram
  5. Send summary with both boleto codes

Usage:
  python3 gps_boleto.py                    # auto-detects previous month
  python3 gps_boleto.py --competencia 03/2026  # explicit month

Environment:
  BROWSERBASE_API_KEY    — Browserbase API key (required)
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

SCRIPT_VERSION = "gps-v1-2026-04-06"
print(f"[INIT] GPS Boleto Generator {SCRIPT_VERSION} at {datetime.now().isoformat()}")

SAL_URL = "https://sal.rfb.gov.br/calculo-contribuicao/contribuintes-2"

CONTRIBUTORS = [
    {"name": "Fernanda", "nit": "11975199574"},
    {"name": "Max", "nit": "13883306818"},
]

PAYMENT_CODE = "1163"  # Contribuinte Individual — Recolhimento Mensal — 11%

TELEGRAM_CHAT_ID = "8409634074"

CAPTCHA_TIMEOUT = 300  # 5 minutes to solve CAPTCHA
CAPTCHA_POLL_INTERVAL = 5  # Check every 5 seconds


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
        # Auto-detect project ID
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

    # Create session
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

    # Get live view URL
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
# CAPTCHA HANDLING
# ============================================================================

def wait_for_captcha(page: Page, live_view_url: str, person_name: str) -> bool:
    """
    Wait for the user to solve the reCAPTCHA via live view.
    Returns True if solved, False if timed out.
    """
    # First, try clicking the CAPTCHA checkbox
    # There are 2 reCAPTCHA iframes: the checkbox (first) and the challenge (second)
    try:
        captcha_frame = page.frame_locator("iframe[title='reCAPTCHA']")
        checkbox = captcha_frame.locator("#recaptcha-anchor")
        if checkbox.is_visible(timeout=5000):
            checkbox.click()
            print("[CAPTCHA] Clicked reCAPTCHA checkbox")
            time.sleep(2)
    except Exception as e:
        print(f"[CAPTCHA] Could not click checkbox: {e}")

    # Check if already solved (green checkmark)
    if _is_captcha_solved(page):
        print("[CAPTCHA] Already solved!")
        return True

    # Notify Fernanda
    send_telegram(
        f"CAPTCHA apareceu para GPS de *{person_name}*! "
        f"Abra o live view e resolva:\n{live_view_url}"
    )

    # Poll until solved or timeout
    start = time.time()
    while time.time() - start < CAPTCHA_TIMEOUT:
        if _is_captcha_solved(page):
            print("[CAPTCHA] Solved!")
            send_telegram(f"CAPTCHA resolvido para {person_name}! Continuando...")
            return True
        time.sleep(CAPTCHA_POLL_INTERVAL)

    print("[CAPTCHA] Timed out")
    send_telegram(f"CAPTCHA timeout para {person_name} (5 min). Pulando.")
    return False


def _is_captcha_solved(page: Page) -> bool:
    """Check if reCAPTCHA has been solved."""
    try:
        # Use exact title to get the checkbox iframe (not the challenge iframe)
        captcha_frame = page.frame_locator("iframe[title='reCAPTCHA']")
        anchor = captcha_frame.locator("#recaptcha-anchor")
        aria_checked = anchor.get_attribute("aria-checked", timeout=2000)
        return aria_checked == "true"
    except Exception:
        pass

    # Alternative: check if the Consultar button (3rd button, index 2) is now enabled
    try:
        consultar = page.locator("button").nth(2)
        if consultar.is_visible(timeout=1000) and consultar.is_enabled(timeout=1000):
            return True
    except Exception:
        pass

    return False


# ============================================================================
# GPS BOLETO GENERATION
# ============================================================================

def generate_boleto_for_person(
    page: Page,
    person: dict,
    competencia: str,
    live_view_url: str,
) -> Optional[dict]:
    """
    Generate a GPS boleto for one person.
    Returns dict with linha_digitavel, valor, vencimento or None on failure.
    """
    name = person["name"]
    nit = person["nit"]
    print(f"\n[GPS] === Starting boleto for {name} (NIT: {nit}) ===")

    # Step 1: Navigate to SAL portal
    print(f"[GPS] Navigating to SAL portal...")
    page.goto(SAL_URL, wait_until="networkidle", timeout=30000)
    time.sleep(2)

    # Take screenshot for debugging
    page.screenshot(path=f"/tmp/gps_{name.lower()}_01_portal.png")

    # Step 2: Select category "Contribuinte Individual" (radio button)
    print(f"[GPS] Selecting category...")
    try:
        # The label intercepts pointer events on the radio input, so click the label directly
        label = page.locator("label[for='categoria_op_AUTONOMO_OU_CONTRIBUINTE_INDIVIDUAL']")
        if label.is_visible(timeout=5000):
            label.click()
            print(f"[GPS] Category selected via label")
        else:
            page.locator("label:has-text('Contribuinte Individual')").first.click()
            print(f"[GPS] Category selected via label text")
    except Exception as e:
        print(f"[GPS] Category selection error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_category.png")

    time.sleep(1)

    # Step 3: Enter NIT (single text input on the page)
    print(f"[GPS] Entering NIT: {nit}")
    try:
        # The NIT input has a dynamic ID like "br-input-XX", so find the only visible text input
        nit_input = page.locator("input[type='text']").first
        if nit_input.is_visible(timeout=5000):
            nit_input.fill(nit)
            print(f"[GPS] NIT entered")
        else:
            print(f"[GPS] NIT input not visible")
    except Exception as e:
        print(f"[GPS] NIT entry error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_nit.png")

    time.sleep(1)
    page.screenshot(path=f"/tmp/gps_{name.lower()}_02_filled.png")

    # Step 4: CAPTCHA
    print(f"[GPS] Handling CAPTCHA...")
    if not wait_for_captcha(page, live_view_url, name):
        return None

    page.screenshot(path=f"/tmp/gps_{name.lower()}_03_captcha_done.png")

    # Step 5: Click Consultar (3rd button on page, index 2 — was disabled before CAPTCHA)
    print(f"[GPS] Clicking Consultar...")
    try:
        # Try text-based first, then fall back to the 3rd button
        consultar = page.locator("button:has-text('Consultar')")
        if consultar.count() == 0:
            # Buttons on this page don't have visible text — use the 3rd button (index 2)
            consultar = page.locator("button").nth(2)
        else:
            consultar = consultar.first
        consultar.click(timeout=5000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception as e:
        print(f"[GPS] Consultar click error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_consultar.png")
        return None

    time.sleep(2)
    page.screenshot(path=f"/tmp/gps_{name.lower()}_04_after_consultar.png")

    # Step 6: Select payment code 1163
    print(f"[GPS] Selecting payment code {PAYMENT_CODE}...")
    try:
        code_select = page.locator("select#cdCodPgto, select[name*='odigo'], select[name*='odPgto']")
        if code_select.is_visible(timeout=5000):
            code_select.select_option(value=PAYMENT_CODE)
            print(f"[GPS] Payment code selected: {PAYMENT_CODE}")
        else:
            # Try text-based selection
            page.locator(f"text={PAYMENT_CODE}").first.click()
    except Exception as e:
        print(f"[GPS] Payment code error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_code.png")

    time.sleep(1)

    # Step 7: Set competência
    print(f"[GPS] Setting competência: {competencia}")
    try:
        comp_input = page.locator("input#competencia, input[name*='ompet'], input[name*='competencia']")
        if comp_input.is_visible(timeout=5000):
            comp_input.fill("")
            comp_input.fill(competencia)
        else:
            inputs = page.locator("input[type='text']")
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                placeholder = inp.get_attribute("placeholder") or ""
                if "mm" in placeholder.lower() or "compet" in placeholder.lower():
                    inp.fill(competencia)
                    print(f"[GPS] Competência set in input #{i}")
                    break
    except Exception as e:
        print(f"[GPS] Competência error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_comp.png")

    time.sleep(1)
    page.screenshot(path=f"/tmp/gps_{name.lower()}_05_code_comp.png")

    # Step 8: Click Calcular / Gerar Guia
    print(f"[GPS] Clicking Calcular/Gerar Guia...")
    try:
        calc_btn = page.locator(
            "button:has-text('Calcular'), input[value='Calcular'], "
            "button:has-text('Gerar'), input[value*='Gerar']"
        )
        calc_btn.first.click(timeout=5000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception as e:
        print(f"[GPS] Calcular click error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_calc.png")
        return None

    time.sleep(3)
    page.screenshot(path=f"/tmp/gps_{name.lower()}_06_boleto.png")

    # Step 9: Extract boleto info
    print(f"[GPS] Extracting boleto info...")
    result = extract_boleto_info(page)

    if result:
        print(f"[GPS] Boleto for {name}: {result}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_07_done.png")
    else:
        print(f"[GPS] Could not extract boleto info for {name}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_extract.png")
        # Send screenshot info
        send_telegram(
            f"Não consegui extrair a linha digitável para {name}. "
            f"Verifique os screenshots em /tmp/gps_{name.lower()}_*.png"
        )

    return result


def extract_boleto_info(page: Page) -> Optional[dict]:
    """Extract linha digitável, valor, and vencimento from the boleto page."""
    page_text = page.inner_text("body")

    # Look for "Linha Digitável" or a 47-48 digit number pattern
    linha = None
    valor = None
    vencimento = None

    # Try to find linha digitável — typically a 47-48 digit number with dots/spaces
    import re

    # Pattern: groups of digits separated by dots or spaces (GPS format)
    # e.g., "85890.00000 00000.000003 00000.000003 0 00000000000000"
    linha_patterns = [
        r"(\d{5}\.?\d{5}\s+\d{5}\.?\d{6}\s+\d{5}\.?\d{6}\s+\d\s+\d{14})",  # Standard GPS format
        r"Linha\s*Digit[áa]vel[:\s]*([0-9\s.]+)",  # After label
        r"(\d[\d\s.]{40,55})",  # Any long digit string
    ]
    for pattern in linha_patterns:
        match = re.search(pattern, page_text, re.IGNORECASE)
        if match:
            linha = match.group(1).strip()
            break

    # Try to find valor
    valor_patterns = [
        r"[Vv]alor[:\s]*R?\$?\s*([\d.,]+)",
        r"R\$\s*([\d.,]+)",
        r"Total[:\s]*R?\$?\s*([\d.,]+)",
    ]
    for pattern in valor_patterns:
        match = re.search(pattern, page_text)
        if match:
            valor = match.group(1).strip()
            break

    # Try to find vencimento
    venc_patterns = [
        r"[Vv]encimento[:\s]*(\d{2}/\d{2}/\d{4})",
        r"[Dd]ata\s*de\s*[Vv]encimento[:\s]*(\d{2}/\d{2}/\d{4})",
        r"(\d{2}/\d{2}/\d{4})",  # Any date
    ]
    for pattern in venc_patterns:
        match = re.search(pattern, page_text)
        if match:
            vencimento = match.group(1).strip()
            break

    if not linha:
        # Last resort: dump page text for debugging
        print(f"[GPS] Page text (first 2000 chars): {page_text[:2000]}")
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
    parser = argparse.ArgumentParser(description="GPS Boleto Generator")
    parser.add_argument("--competencia", type=str, help="Competência in MM/YYYY format (default: previous month)")
    parser.add_argument("--person", type=str, help="Generate for one person only: 'fernanda' or 'max'")
    args = parser.parse_args()

    competencia = args.competencia or get_competencia()
    print(f"[GPS] Competência: {competencia}")

    # Filter to specific person if requested
    people = CONTRIBUTORS
    if args.person:
        people = [p for p in CONTRIBUTORS if p["name"].lower() == args.person.lower()]
        if not people:
            print(f"[GPS] Unknown person: {args.person}")
            sys.exit(1)

    # Step 1: Create Browserbase session
    try:
        session = create_browserbase_session()
    except Exception as e:
        msg = f"Falha ao criar sessão Browserbase: {e}"
        print(f"[GPS] {msg}")
        send_telegram(f"GPS Boleto falhou: {msg}")
        sys.exit(1)

    live_view_url = session["liveViewUrl"]
    connect_url = session["connectUrl"]

    if not connect_url:
        msg = "Browserbase session created but no connectUrl returned"
        print(f"[GPS] {msg}")
        send_telegram(f"GPS Boleto falhou: {msg}")
        sys.exit(1)

    # Step 2: Notify Fernanda
    send_telegram(
        f"GPS Boleto — competência *{competencia}*\n\n"
        f"Sessão Browserbase criada! Live view:\n{live_view_url}\n\n"
        f"Abra este link para resolver o CAPTCHA quando aparecer."
    )

    # Step 3: Connect Playwright to Browserbase
    results = []
    try:
        with sync_playwright() as pw:
            print(f"[GPS] Connecting to Browserbase: {connect_url[:80]}...")
            browser = pw.chromium.connect_over_cdp(connect_url, timeout=30000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            for person in people:
                try:
                    result = generate_boleto_for_person(page, person, competencia, live_view_url)
                    if result:
                        result["name"] = person["name"]
                        results.append(result)

                        # Send individual result
                        send_telegram(
                            f"✅ GPS *{person['name']}* — {competencia}\n"
                            f"Linha digitável: `{result['linha_digitavel']}`\n"
                            f"Valor: R$ {result['valor']}\n"
                            f"Vencimento: {result['vencimento']}"
                        )
                    else:
                        send_telegram(
                            f"❌ GPS {person['name']} — {competencia}: "
                            f"Não consegui gerar o boleto. Verifique os screenshots."
                        )
                except Exception as e:
                    print(f"[GPS] Error for {person['name']}: {e}")
                    traceback.print_exc()
                    send_telegram(f"❌ GPS {person['name']} erro: {e}")

            browser.close()

    except Exception as e:
        msg = f"Playwright/Browserbase connection failed: {e}"
        print(f"[GPS] {msg}")
        traceback.print_exc()
        send_telegram(f"GPS Boleto falhou: {msg}")
        sys.exit(1)

    # Step 4: Summary
    if results:
        summary_lines = [f"📋 *GPS Boleto Summary — {competencia}*\n"]
        for r in results:
            summary_lines.append(
                f"*{r['name']}*:\n"
                f"  Linha: `{r['linha_digitavel']}`\n"
                f"  Valor: R$ {r['valor']}\n"
                f"  Vencimento: {r['vencimento']}\n"
            )
        send_telegram("\n".join(summary_lines))
        print(f"\n[GPS] Done! {len(results)}/{len(people)} boletos generated.")
    else:
        send_telegram(f"GPS Boleto — {competencia}: Nenhum boleto gerado. Verifique os logs.")
        print(f"\n[GPS] Failed — no boletos generated.")
        sys.exit(1)


if __name__ == "__main__":
    main()
