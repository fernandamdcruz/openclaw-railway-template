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

_missing = []
for pkg in ["requests", "playwright"]:
    try:
        __import__(pkg)
    except ImportError:
        _missing.append(pkg)
if _missing:
    print(f"[FATAL] Missing packages: {', '.join(_missing)}. Fix the Dockerfile.")
    sys.exit(1)

import requests
from playwright.sync_api import sync_playwright, Page, Browser

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_VERSION = "gps-v2-2026-04-06"
print(f"[INIT] GPS Boleto Generator {SCRIPT_VERSION} at {datetime.now().isoformat()}")

SAL_URL = "https://sal.rfb.gov.br/calculo-contribuicao/contribuintes-2"

CONTRIBUTORS = [
    {"name": "Fernanda", "nit": "11975199574"},
    {"name": "Max", "nit": "13883306818"},
]

PAYMENT_CODE = "1163"  # Contribuinte Individual — Recolhimento Mensal — 11%
SALARIO_MINIMO = "1621,00"  # 2026 minimum wage

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
    page.goto(SAL_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)  # SAL portal has polling — networkidle never fires

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

    # Steps 5-6 (merged): Advance from CAPTCHA to the contributions page.
    # After the CAPTCHA is solved the form may land in any of three states:
    #   (a) Still on the portal — Consultar visible, needs click
    #   (b) On the verification page — "Verifique os dados" + Confirmar
    #   (c) Straight to the contributions page — "Adicionar" button visible
    # We also have to survive the <app-loading><br-scrim show> overlay that
    # briefly blocks clicks after each transition.

    def _wait_scrim_hidden(timeout=10000):
        try:
            page.locator("br-scrim[show]").first.wait_for(state="hidden", timeout=timeout)
        except Exception:
            pass

    def _on_contributions_page():
        return (
            page.locator('br-button[title="Adicionar"]').count() > 0
            or page.get_by_text("Nenhuma contribuição informada", exact=False).count() > 0
            or page.locator('br-select[label="Código Pagamento"]').count() > 0
        )

    def _on_verification_page():
        return (
            page.get_by_text("Verifique os dados", exact=False).count() > 0
            or page.get_by_text("Nova Consulta", exact=False).count() > 0
        )

    _wait_scrim_hidden()

    # Step 5: Consultar if we're still on the portal
    if _on_contributions_page():
        print(f"[GPS] Already on contributions page — skipping Consultar + verify")
    elif _on_verification_page():
        print(f"[GPS] On verification page — skipping Consultar")
    else:
        print(f"[GPS] Clicking Consultar...")
        try:
            page.get_by_text("Consultar", exact=True).click(timeout=10000)
        except Exception as e:
            # The click may have succeeded despite the exception (e.g. Angular
            # detached the element after handling it). Poll page state before
            # giving up.
            print(f"[GPS] Consultar click threw {e.__class__.__name__} — checking page state")
        time.sleep(3)
        _wait_scrim_hidden()
        if not _on_contributions_page() and not _on_verification_page():
            print(f"[GPS] Not on expected page after Consultar")
            page.screenshot(path=f"/tmp/gps_{name.lower()}_err_consultar.png")
            return None

    page.screenshot(path=f"/tmp/gps_{name.lower()}_04_after_consultar.png")

    # Step 6: Confirmar on verification page (if that's where we ended up)
    if _on_verification_page() and not _on_contributions_page():
        print(f"[GPS] Clicking Confirmar on verification page...")
        try:
            page.get_by_text("Confirmar", exact=True).first.click(timeout=10000)
        except Exception as e:
            print(f"[GPS] Verification Confirmar click threw {e.__class__.__name__} — checking page state")
        time.sleep(3)
        _wait_scrim_hidden()
        if not _on_contributions_page():
            print(f"[GPS] Not on contributions page after verification Confirmar")
            page.screenshot(path=f"/tmp/gps_{name.lower()}_err_verify.png")
            return None
        print(f"[GPS] Verification confirmed — on contributions page")

    page.screenshot(path=f"/tmp/gps_{name.lower()}_05_after_verify.png")

    # Step 7: Select payment code 1163 from <br-select> dropdown
    print(f"[GPS] Selecting payment code {PAYMENT_CODE}...")
    try:
        br_select = page.locator('br-select[label="Código Pagamento"]')
        if not br_select.is_visible(timeout=3000):
            br_select = page.locator("br-select").first
        br_select.click()
        time.sleep(1)
        page.get_by_text(PAYMENT_CODE, exact=False).first.click()
        time.sleep(1)
        print(f"[GPS] Payment code {PAYMENT_CODE} selected")
    except Exception as e:
        print(f"[GPS] Payment code error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_code.png")
        return None

    page.screenshot(path=f"/tmp/gps_{name.lower()}_06_code_selected.png")

    # Step 8: Click "+ Adicionar" button (br-button web component)
    print(f"[GPS] Clicking Adicionar...")
    try:
        page.locator('br-button[title="Adicionar"]').click(timeout=5000)
        time.sleep(2)
        print(f"[GPS] Adicionar modal opened")
    except Exception as e:
        # Fallback: try text match
        try:
            page.get_by_text("Adicionar", exact=True).click(timeout=3000)
            time.sleep(2)
            print(f"[GPS] Adicionar clicked via text")
        except Exception as e2:
            print(f"[GPS] Adicionar error: {e2}")
            page.screenshot(path=f"/tmp/gps_{name.lower()}_err_adicionar.png")
            return None

    page.screenshot(path=f"/tmp/gps_{name.lower()}_07_adicionar_modal.png")

    # Step 9: Fill competência (MM/AAAA) and salário in the modal
    print(f"[GPS] Filling competência: {competencia} and salário: {SALARIO_MINIMO}...")
    try:
        comp_input = page.locator('input[placeholder="mm/aaaa"]')
        comp_input.fill(competencia)
        print(f"[GPS] Competência filled: {competencia}")

        sal_input = page.locator('#input-salario')
        sal_input.fill(SALARIO_MINIMO)
        # The br-input currency field sometimes doesn't validate after fill().
        # Workaround: delete the last char and retype it to trigger validation.
        sal_input.press("End")
        sal_input.press("Backspace")
        sal_input.type("0", delay=50)
        print(f"[GPS] Salário filled: {SALARIO_MINIMO}")
    except Exception as e:
        print(f"[GPS] Fill error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_fill.png")
        return None

    time.sleep(1)
    page.screenshot(path=f"/tmp/gps_{name.lower()}_08_form_filled.png")

    # Step 10: Click modal "Confirmar" (first of two — modal is on top)
    print(f"[GPS] Confirming contribution in modal...")
    try:
        page.get_by_text("Confirmar", exact=True).first.click(timeout=5000)
        time.sleep(2)
        # Wait for the br-scrim overlay to disappear (Angular transition)
        try:
            page.locator("br-scrim[show]").wait_for(state="hidden", timeout=5000)
        except Exception:
            time.sleep(2)  # fallback wait
        print(f"[GPS] Modal confirmed — contribution added to list")
    except Exception as e:
        print(f"[GPS] Modal confirm error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_modal_confirm.png")
        return None

    page.screenshot(path=f"/tmp/gps_{name.lower()}_09_contribution_added.png")

    # Step 10b: Set "Data do Pagamento" to a valid weekday — the field defaults
    # to today, and SAL rejects weekend dates with "Data de pagamento não pode
    # ser Sábado ou Domingo". We compute the canonical due date (15th of the
    # month after competência, rolled forward off weekends).
    payment_date = compute_payment_date(competencia)
    print(f"[GPS] Setting Data do Pagamento to {payment_date}...")
    try:
        # The field is a native HTML5 date input. It sits in the "Dados do Pagamento"
        # section, alongside the "Código Pagamento" dropdown. Target via input[type=date]
        # which is unique on this page.
        date_input = page.locator('input[type="date"]').first
        date_input.fill(payment_date)
        date_input.press("Tab")  # blur to commit the value
        time.sleep(1)
        print(f"[GPS] Data do Pagamento set: {payment_date}")
    except Exception as e:
        print(f"[GPS] Data do Pagamento set error (proceeding anyway): {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_payment_date.png")

    # Step 11: Click page-level "Confirmar" (last of the visible Confirmar buttons).
    # Wait for any lingering app-loading scrim to clear first (it can appear
    # after the modal Confirmar, or after the Data do Pagamento field commits).
    print(f"[GPS] Clicking page-level Confirmar...")
    _wait_scrim_hidden(timeout=10000)
    try:
        page.get_by_text("Confirmar", exact=True).last.click(timeout=10000)
    except Exception as e:
        # Angular may detach the button after the click succeeds. Poll for the
        # next-page marker (a checkbox row) before treating this as an error.
        print(f"[GPS] Page confirm click threw {e.__class__.__name__} — checking page state")
    time.sleep(3)
    _wait_scrim_hidden(timeout=10000)
    if page.locator('input[type="checkbox"]').count() == 0:
        print(f"[GPS] Page confirm did not advance to checkbox page")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_page_confirm.png")
        return None
    print(f"[GPS] Page confirmed — on checkbox/selection page")

    page.screenshot(path=f"/tmp/gps_{name.lower()}_10_selection_page.png")

    # Step 12: Checkbox page — "Seleção de Competências"
    # Check the row checkbox (uses <br-checkbox> wrapping native <input>)
    print(f"[GPS] Checking competência checkbox...")
    try:
        # Get all checkbox inputs — last one is the row, first is "select all"
        checkboxes = page.locator('input[type="checkbox"]')
        cb_count = checkboxes.count()
        print(f"[GPS] Found {cb_count} checkboxes")
        # Check the last checkbox (the row for our competência)
        if cb_count > 0:
            last_cb = checkboxes.last
            last_cb.check(force=True)
            print(f"[GPS] Checkbox checked")
        time.sleep(1)
    except Exception as e:
        print(f"[GPS] Checkbox error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_checkbox.png")
        return None

    page.screenshot(path=f"/tmp/gps_{name.lower()}_11_checkbox_checked.png")

    # Step 13: Click "Emitir GPS"
    print(f"[GPS] Clicking Emitir GPS...")
    try:
        page.get_by_text("Emitir GPS", exact=True).click(timeout=5000)
        time.sleep(5)
        print(f"[GPS] Emitir GPS clicked — boleto should be generated")
    except Exception as e:
        print(f"[GPS] Emitir GPS error: {e}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_emitir.png")
        return None

    page.screenshot(path=f"/tmp/gps_{name.lower()}_12_boleto.png")

    # Step 14: Extract boleto info
    print(f"[GPS] Extracting boleto info...")
    result = extract_boleto_info(page)

    if result:
        print(f"[GPS] Boleto for {name}: {result}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_13_done.png")
    else:
        print(f"[GPS] Could not extract boleto info for {name}")
        page.screenshot(path=f"/tmp/gps_{name.lower()}_err_extract.png")
        send_telegram(
            f"Não consegui extrair a linha digitável para {name}. "
            f"Verifique os screenshots em /tmp/gps_{name.lower()}_*.png"
        )

    return result


def extract_boleto_info(page: Page) -> Optional[dict]:
    """Extract linha digitável, valor, and vencimento from the boleto result page."""
    import re

    # SAL portal is Angular — inner_text("body") can return empty.
    # Try multiple extraction methods.
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
        # Last resort: get all text from all elements
        try:
            texts = page.locator("*").all_text_contents()
            page_text = " ".join(t.strip() for t in texts if t.strip())
        except Exception:
            pass

    print(f"[GPS] Extracted {len(page_text)} chars of page text")
    print(f"[GPS] Page text (first 1000 chars): {page_text[:1000]}")

    linha = None
    valor = None
    vencimento = None

    # GPS linha digitável format: "85810000001-3 78310270116-1 30001197519-8 95742026033-2"
    # Pattern: 4 groups of digits-digit separated by spaces
    linha_patterns = [
        r"(\d{11}-\d\s+\d{11}-\d\s+\d{11}-\d\s+\d{11}-\d)",  # GPS format with dashes
        r"(\d{12}\s+\d{12}\s+\d{12}\s+\d{12})",  # GPS format without dashes
        r"(\d{5}\.?\d{5}\s+\d{5}\.?\d{6}\s+\d{5}\.?\d{6}\s+\d\s+\d{14})",  # Standard boleto
        r"Linha\s*Digit[áa]vel[:\s]*([0-9\s.\-]+)",  # After label
        r"(\d[\d\s.\-]{40,60}\d)",  # Any long digit string with separators
    ]
    for pattern in linha_patterns:
        match = re.search(pattern, page_text, re.IGNORECASE)
        if match:
            linha = match.group(1).strip()
            break

    # If text extraction failed, try reading specific elements
    if not linha:
        try:
            # The linha digitável might be in a specific container
            # Try to find elements containing digit patterns
            all_elements = page.locator("span, div, p, td")
            for i in range(min(all_elements.count(), 100)):
                el_text = all_elements.nth(i).text_content() or ""
                for pattern in linha_patterns:
                    match = re.search(pattern, el_text)
                    if match:
                        linha = match.group(1).strip()
                        break
                if linha:
                    break
        except Exception as e:
            print(f"[GPS] Element scan error: {e}")

    # Extract valor
    valor_patterns = [
        r"Total[:\s]*R?\$?\s*([\d.,]+)",
        r"R\$\s*([\d.,]+)",
        r"[Vv]alor[:\s]*R?\$?\s*([\d.,]+)",
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
        print(f"[GPS] Could not find linha digitável in page text")
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


def compute_payment_date(competencia_mmYYYY: str) -> str:
    """Compute a valid Data do Pagamento for the GPS boleto.

    For code 1163 (INSS contribuinte individual) the due date is the 15th
    of the month following competência. If the 15th is a weekend, push to
    the next Monday. If today is already past that date, use tomorrow
    (rolled forward to the next weekday). The SAL portal rejects weekend
    payment dates ("Data de pagamento não pode ser Sábado ou Domingo").

    Returns YYYY-MM-DD (the format HTML5 date inputs accept via .fill()).
    """
    mm, yyyy = competencia_mmYYYY.split("/")
    month = int(mm)
    year = int(yyyy)
    if month == 12:
        due_month, due_year = 1, year + 1
    else:
        due_month, due_year = month + 1, year

    from datetime import date as _date
    due = _date(due_year, due_month, 15)
    today = _date.today()

    # If the canonical due date is already in the past, use tomorrow.
    if due < today:
        due = today + timedelta(days=1)

    # Roll forward off weekends (Mon=0..Sun=6).
    while due.weekday() >= 5:
        due += timedelta(days=1)

    return due.strftime("%Y-%m-%d")


def main():
    parser = argparse.ArgumentParser(description="GPS Boleto Generator")
    parser.add_argument("--competencia", type=str, help="Competência in MM/YYYY format (default: previous month)")
    parser.add_argument("--person", type=str, help="Generate for one person only: 'fernanda' or 'max'")
    parser.add_argument("--local", action="store_true", help="Use local Chrome (CDP on localhost:9222) instead of Browserbase")
    parser.add_argument("--cdp-url", type=str, default="http://localhost:9222", help="CDP URL for --local mode (default: http://localhost:9222)")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram notifications (for local testing)")
    args = parser.parse_args()

    competencia = args.competencia or get_competencia()
    print(f"[GPS] Competência: {competencia}")

    # Override send_telegram if --no-telegram
    if args.no_telegram:
        global send_telegram
        _orig_send = send_telegram
        def send_telegram(msg):
            print(f"[TG-SKIP] {msg}")

    # Filter to specific person if requested
    people = CONTRIBUTORS
    if args.person:
        people = [p for p in CONTRIBUTORS if p["name"].lower() == args.person.lower()]
        if not people:
            print(f"[GPS] Unknown person: {args.person}")
            sys.exit(1)

    if args.local:
        # Local Chrome mode — connect to existing Chrome with CDP
        connect_url = args.cdp_url
        live_view_url = "(local Chrome — visible on your desktop)"
        print(f"[GPS] Local mode: connecting to {connect_url}")
    else:
        # Browserbase mode
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

        # Notify Fernanda
        send_telegram(
            f"GPS Boleto — competência *{competencia}*\n\n"
            f"Sessão Browserbase criada! Live view:\n{live_view_url}\n\n"
            f"Abra este link para resolver o CAPTCHA quando aparecer."
        )

    # Connect Playwright
    results = []
    try:
        with sync_playwright() as pw:
            print(f"[GPS] Connecting to: {connect_url[:80]}...")
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
        msg = f"Playwright connection failed: {e}"
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
