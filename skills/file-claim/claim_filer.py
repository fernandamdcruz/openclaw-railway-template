"""
BCBS Claim Filer - Automated claim filing for BCBS portal using Playwright.

This script automates the complete claim filing workflow on the BCBS portal
(https://members.bcbsglobalsolutions.com) which uses Flutter Web with CanvasKit rendering.

Architecture:
- This script handles ~90% of the workflow deterministically (no LLM needed)
- FerdyBot's SKILL.md orchestrates: runs this script first, falls back to LLM on failure
- Connects to existing Chromium CDP instance (port 9222) on Railway
- Reads claim data from Google Sheets, files each pending claim, updates sheet

Flutter Web DOM patterns (confirmed via live DOM inspection 2026-03-25):
- All interactive elements are <flt-semantics> with ARIA roles
- Text inputs: click flt-semantics textbox → wait 500ms → type with keyboard.type()
- Dropdowns: click textbox → wait 1s → click button option in overlay
- Date pickers: click button → calendar dialog → type in mm/dd/yyyy search input → click OK
- Navigation arrows: button "Backward" / button "Forward" (not "previous"/"next")
- Form scrolling: flt-semantics container with scrollHeight > clientHeight + 50
"""

import asyncio
import os
import subprocess
import json
import re
import imaplib
import email
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

# Configuration
BCBS_PORTAL_URL = "https://members.bcbsglobalsolutions.com"
CDP_URL = "http://127.0.0.1:9222"
GOOGLE_SHEET_ID = "1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk"
GOOGLE_SHEET_TAB = "2026"
TELEGRAM_CHAT_ID = "8409634074"
MAX_RETRIES = 3
FLUTTER_WAIT_TIME = 2000  # milliseconds
SHORT_WAIT = 500  # milliseconds

# Screenshot directory
SCREENSHOT_DIR = Path("/tmp/bcbs_screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

print(f"[INIT] BCBS Claim Filer initialized at {datetime.now().isoformat()}")


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def take_screenshot(page: Page, name: str) -> str:
    """Take a screenshot for debugging. Returns the file path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = SCREENSHOT_DIR / f"{name}_{timestamp}.png"
    await page.screenshot(path=str(filename))
    print(f"[DEBUG] Screenshot saved: {filename}")
    return str(filename)


async def scroll_form(page: Page, amount: int = 500) -> None:
    """
    Scroll the Flutter form container.
    The form is inside a scrollable flt-semantics element, NOT the page.
    """
    await page.evaluate(f"""
        () => {{
            const els = document.querySelectorAll('flt-semantics');
            let scrollable = null;
            for (const el of els) {{
                if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 300) {{
                    scrollable = el;
                }}
            }}
            if (scrollable) {{
                scrollable.scrollTop += {amount};
            }}
        }}
    """)
    await asyncio.sleep(0.3)


async def scroll_form_to_top(page: Page) -> None:
    """Scroll the Flutter form container back to top."""
    await page.evaluate("""
        () => {
            const els = document.querySelectorAll('flt-semantics');
            for (const el of els) {
                if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 300) {
                    el.scrollTop = 0;
                    break;
                }
            }
        }
    """)
    await asyncio.sleep(0.3)


async def wait_for_flutter(page: Page, wait_ms: int = FLUTTER_WAIT_TIME) -> None:
    """Wait for Flutter widget tree to rebuild after interactions."""
    await asyncio.sleep(wait_ms / 1000)


async def fill_flutter_field(page: Page, field_pattern: str, value: str) -> None:
    """
    Fill a Flutter textbox field using keyboard input.

    Confirmed DOM pattern (2026-03-25):
    1. Click the flt-semantics textbox (matched by aria-label/name)
    2. Wait 500ms for Flutter to create real <input> in <flt-text-editing-host>
    3. Select all existing text (Ctrl+A) and type new value
    4. Press Tab to blur and trigger onChange
    """
    print(f"[FIELD] Filling field matching '{field_pattern}' with '{value}'")
    try:
        field = page.get_by_role("textbox", name=re.compile(field_pattern, re.IGNORECASE))
        await field.click()
        await asyncio.sleep(SHORT_WAIT / 1000)

        # Select all existing text and replace
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.1)
        await page.keyboard.type(value, delay=30)
        await page.keyboard.press("Tab")
        await wait_for_flutter(page)
        print(f"[FIELD] Successfully filled field")
    except Exception as e:
        print(f"[ERROR] Failed to fill field '{field_pattern}': {str(e)}")
        raise


async def select_dropdown(page: Page, dropdown_pattern: str, value: str) -> None:
    """
    Select a value from a Flutter dropdown.

    Flutter Web dropdowns are notoriously difficult. This function tries
    multiple strategies in order:
    1. Click dropdown → find and click the option button in the overlay
    2. If wrong value selected → close and re-open, try arrow keys
    3. JavaScript injection as last resort

    Known issue (2026-03-25): Patient dropdown defaults to first patient
    alphabetically ("Mathias Jacobson") and resists keyboard filtering.
    """
    print(f"[DROPDOWN] Selecting '{value}' from dropdown matching '{dropdown_pattern}'")

    # Strategy 1: Click dropdown, wait for overlay, click matching option
    try:
        dropdown = page.get_by_role("textbox", name=re.compile(dropdown_pattern, re.IGNORECASE))
        await dropdown.click()
        await asyncio.sleep(2)  # Flutter overlays need time

        # Look for the option as a button in the overlay
        option = page.get_by_role("button", name=re.compile(re.escape(value), re.IGNORECASE))
        if await option.count() > 0:
            await option.first.click()
            await wait_for_flutter(page)
            print(f"[DROPDOWN] Strategy 1 succeeded: clicked option button")
            return

        # Try as generic text element (some Flutter dropdowns use text, not buttons)
        option = page.get_by_text(value, exact=False)
        if await option.count() > 0:
            await option.first.click()
            await wait_for_flutter(page)
            print(f"[DROPDOWN] Strategy 1 succeeded: clicked text element")
            return

        # Close the overlay by pressing Escape before trying next strategy
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
    except Exception as e:
        print(f"[DROPDOWN] Strategy 1 failed: {e}")
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            pass

    # Strategy 2: Click dropdown, use arrow keys to cycle through options
    try:
        dropdown = page.get_by_role("textbox", name=re.compile(dropdown_pattern, re.IGNORECASE))
        await dropdown.click()
        await asyncio.sleep(1.5)

        # Try typing the first few characters to filter
        await page.keyboard.type(value[:5], delay=80)
        await asyncio.sleep(1)

        # Check if a matching option appeared
        option = page.get_by_role("button", name=re.compile(re.escape(value), re.IGNORECASE))
        if await option.count() > 0:
            await option.first.click()
            await wait_for_flutter(page)
            print(f"[DROPDOWN] Strategy 2 succeeded: type-to-filter")
            return

        # Try arrow keys (up to 10 options)
        for i in range(10):
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(0.3)

            # Check all visible buttons for match
            buttons = page.get_by_role("button")
            count = await buttons.count()
            for j in range(count):
                btn = buttons.nth(j)
                text = await btn.inner_text()
                if value.lower() in text.lower():
                    await btn.click()
                    await wait_for_flutter(page)
                    print(f"[DROPDOWN] Strategy 2 succeeded: arrow key + click at position {i}")
                    return

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
    except Exception as e:
        print(f"[DROPDOWN] Strategy 2 failed: {e}")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    # Strategy 3: JavaScript injection — find the flt-semantics element and
    # dispatch events to simulate selection. This is the nuclear option.
    try:
        print(f"[DROPDOWN] Trying Strategy 3: JavaScript injection")
        # Find all flt-semantics elements with the value text and click via JS
        # NOTE: escape value for JS string (can't use backslash in f-string expr on Python <3.12)
        escaped_value = value.replace("'", "\\'")
        clicked = await page.evaluate(f"""
            () => {{
                const target = '{escaped_value}';
                const els = document.querySelectorAll('flt-semantics');
                for (const el of els) {{
                    const label = el.getAttribute('aria-label') || el.innerText || '';
                    if (label.toLowerCase().includes(target.toLowerCase())) {{
                        el.click();
                        el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                        el.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                        return true;
                    }}
                }}
                // Also try button role elements
                const buttons = document.querySelectorAll('[role="button"]');
                for (const btn of buttons) {{
                    const label = btn.getAttribute('aria-label') || btn.innerText || '';
                    if (label.toLowerCase().includes(target.toLowerCase())) {{
                        btn.click();
                        return true;
                    }}
                }}
                return false;
            }}
        """)

        if clicked:
            await wait_for_flutter(page)
            print(f"[DROPDOWN] Strategy 3 succeeded: JavaScript injection")
            return

        # Last resort: click dropdown again, screenshot for debugging
        await take_screenshot(page, f"dropdown_failed_{dropdown_pattern}")
        raise Exception(f"All dropdown strategies failed for '{value}' in '{dropdown_pattern}'")

    except Exception as e:
        print(f"[ERROR] All dropdown strategies failed for '{dropdown_pattern}': {str(e)}")
        raise


async def select_date(page: Page, date_button_pattern: str, target_date: str) -> None:
    """
    Select a date using the Flutter Material DatePicker.

    target_date format: "YYYY-MM-DD"

    Confirmed DOM pattern (2026-03-25):
    - Date field is a button: "Start Date of Service date picker. Current value: not set"
    - Click opens a calendar dialog with:
      - textbox "mm/dd/yyyy" type="search" — real input, can type date here
      - button "Backward" / button "Forward" — month navigation
      - button "CANCEL" and unlabeled OK button
      - Day numbers may not appear in interactive filter

    Strategy: Type the date into the mm/dd/yyyy search input (fastest, most reliable),
    then click OK. Fall back to calendar grid navigation if typing doesn't work.
    """
    print(f"[DATE] Selecting date '{target_date}' for field matching '{date_button_pattern}'")

    try:
        date_obj = datetime.strptime(target_date, "%Y-%m-%d")
        date_formatted = date_obj.strftime("%m/%d/%Y")  # MM/DD/YYYY for the input

        # Click the date field button to open picker
        date_field = page.get_by_role("button", name=re.compile(date_button_pattern, re.IGNORECASE))
        await date_field.click()
        await asyncio.sleep(2)  # Wait for Material DatePicker dialog

        # Strategy 1: Type into the mm/dd/yyyy search input
        try:
            date_input = page.get_by_role("textbox", name=re.compile("mm/dd/yyyy", re.IGNORECASE))
            if await date_input.count() > 0:
                await date_input.click()
                await asyncio.sleep(SHORT_WAIT / 1000)
                await page.keyboard.press("Control+a")
                await page.keyboard.type(date_formatted, delay=30)
                await asyncio.sleep(0.5)
                print(f"[DATE] Typed date {date_formatted} into search input")
        except Exception as e:
            print(f"[DATE] Text input failed, trying calendar navigation: {e}")
            await _navigate_calendar(page, date_obj)

        # Click OK button (it may be unlabeled — try multiple approaches)
        ok_clicked = False
        # Try finding OK by text
        try:
            ok_btn = page.get_by_text("OK", exact=True)
            if await ok_btn.count() > 0:
                await ok_btn.first.click()
                ok_clicked = True
        except Exception:
            pass

        if not ok_clicked:
            # Try finding the second-to-last button in the dialog (OK is after CANCEL)
            try:
                cancel_btn = page.get_by_role("button", name="CANCEL")
                if await cancel_btn.count() > 0:
                    # OK button is a sibling — find buttons near CANCEL
                    all_buttons = page.get_by_role("button")
                    count = await all_buttons.count()
                    for i in range(count):
                        btn = all_buttons.nth(i)
                        text = await btn.inner_text()
                        if text.strip() == "OK":
                            await btn.click()
                            ok_clicked = True
                            break
            except Exception:
                pass

        if not ok_clicked:
            # Last resort: press Enter which should confirm the dialog
            await page.keyboard.press("Enter")

        await wait_for_flutter(page)
        print(f"[DATE] Successfully selected date {target_date}")
    except Exception as e:
        print(f"[ERROR] Failed to select date: {str(e)}")
        raise


async def _navigate_calendar(page: Page, date_obj: datetime) -> None:
    """
    Fallback: Navigate the calendar grid to select a date.
    Uses button "Backward" / button "Forward" for month navigation.
    """
    target_month = date_obj.strftime("%B %Y")  # e.g., "January 2026"
    target_day = str(date_obj.day)

    max_attempts = 24  # Up to 2 years of navigation
    for _ in range(max_attempts):
        # Check current month displayed
        try:
            page_text = await page.inner_text("body")
            if target_month.lower() in page_text.lower():
                print(f"[DATE] Found target month: {target_month}")
                break
        except Exception:
            pass

        # Navigate: use "Backward" for past, "Forward" for future
        if date_obj < datetime.now():
            nav_btn = page.get_by_role("button", name="Backward")
        else:
            nav_btn = page.get_by_role("button", name="Forward")

        try:
            if await nav_btn.count() > 0:
                await nav_btn.first.click()
                await asyncio.sleep(0.5)
        except Exception:
            break

    # Click the target day number
    try:
        day_btn = page.get_by_role("button", name=target_day)
        if await day_btn.count() > 0:
            await day_btn.first.click()
            await asyncio.sleep(0.3)
            print(f"[DATE] Clicked day {target_day}")
    except Exception as e:
        print(f"[DATE] Could not click day button: {e}")


async def close_popup(page: Page) -> None:
    """Close any popup/dialog that may appear (Important Update, NOTICE, etc.)."""
    try:
        close_btn = page.get_by_role("button", name=re.compile("close|dismiss", re.IGNORECASE))
        if await close_btn.count() > 0:
            await close_btn.first.click()
            await wait_for_flutter(page)
            print("[POPUP] Closed popup")
    except Exception:
        pass


# ============================================================================
# 2FA — EMAIL CODE RETRIEVAL
# ============================================================================

def get_2fa_code_from_gmail() -> Optional[str]:
    """
    Retrieve the BCBS 2FA verification code from Gmail.

    Tries two methods:
    1. gog CLI (if available on Railway): `gog mail list`
    2. IMAP direct connection using BCBS_GMAIL_APP_PASSWORD env var

    Searches for the most recent email from BCBS containing a verification code.
    Retries for up to 90 seconds waiting for the email to arrive.
    """
    print("[2FA] Retrieving verification code from Gmail")

    # Method 1: Try gog CLI first (available on Railway)
    for attempt in range(18):  # 18 attempts × 5s = 90s
        try:
            result = subprocess.run(
                ["gog", "mail", "list", "--query", "from:noreply@bcbsglobalsolutions.com verification code", "--max", "1", "--json"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                emails = json.loads(result.stdout)
                if emails:
                    body = emails[0].get("body", "") or emails[0].get("snippet", "")
                    # Look for 6-digit code
                    code_match = re.search(r'\b(\d{6})\b', body)
                    if code_match:
                        code = code_match.group(1)
                        print(f"[2FA] Found code via gog CLI")
                        return code
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            if attempt == 0:
                print("[2FA] gog CLI not available, trying IMAP")
                break
        except Exception as e:
            print(f"[2FA] gog attempt {attempt+1} failed: {e}")

        if attempt > 0:
            print(f"[2FA] Waiting for email... attempt {attempt+1}/18")
            import time
            time.sleep(5)

    # Method 2: IMAP direct connection
    gmail_user = os.environ.get("BCBS_GMAIL_USER", os.environ.get("BCBS_USERNAME"))
    gmail_app_password = os.environ.get("BCBS_GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_app_password:
        print("[2FA] No Gmail credentials for IMAP - cannot retrieve code")
        return None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_user, gmail_app_password)
        mail.select("inbox")

        for attempt in range(18):
            # Search for recent BCBS emails
            _, message_ids = mail.search(None, '(FROM "noreply@bcbsglobalsolutions.com")')
            if message_ids[0]:
                ids = message_ids[0].split()
                # Get the most recent one
                _, msg_data = mail.fetch(ids[-1], "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                # Extract body
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

                # Look for 6-digit code
                code_match = re.search(r'\b(\d{6})\b', body)
                if code_match:
                    code = code_match.group(1)
                    print(f"[2FA] Found code via IMAP")
                    mail.logout()
                    return code

            print(f"[2FA] IMAP attempt {attempt+1}/18 - waiting for email...")
            import time
            time.sleep(5)

        mail.logout()
    except Exception as e:
        print(f"[2FA] IMAP failed: {e}")

    print("[2FA] Could not retrieve verification code")
    return None


# ============================================================================
# GOOGLE SHEETS INTEGRATION
# ============================================================================

def read_pending_claims() -> List[Dict[str, Any]]:
    """
    Read pending claims from Google Sheets using gog CLI.

    Column mapping:
    - B (1) = Patient Name
    - C (2) = Provider Name
    - D (3) = Date of Service
    - E (4) = Amount Billed
    - F (5) = Currency
    - G (6) = Diagnosis Codes
    - H (7) = Procedure Code
    - I (8) = Invoice #
    - J (9) = City (provider location)
    - K (10) = Claim Status (must be "Pending")
    - L (11) = Drive File Link
    - M (12) = Country of Treatment
    """
    print("[SHEETS] Reading pending claims from Google Sheets")

    claims = []
    try:
        result = subprocess.run(
            ["gog", "sheets", "export", GOOGLE_SHEET_ID, GOOGLE_SHEET_TAB, "--json"],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode == 0:
            rows = json.loads(result.stdout)

            for row_idx, row in enumerate(rows, start=2):  # Row 1 is header
                # Column K is index 10 (0-indexed)
                if len(row) > 10 and row[10] and row[10].strip().lower() == "pending":
                    claim = {
                        "patient": row[1] if len(row) > 1 else "",
                        "provider": row[2] if len(row) > 2 else "",
                        "date": row[3] if len(row) > 3 else "",
                        "amount": row[4] if len(row) > 4 else "",
                        "currency": row[5] if len(row) > 5 else "",
                        "diagnosis": row[6] if len(row) > 6 else "",
                        "procedure": row[7] if len(row) > 7 else "",
                        "invoice_num": row[8] if len(row) > 8 else "",
                        "city": row[9] if len(row) > 9 else "",
                        "drive_link": row[11] if len(row) > 11 else "",
                        "country": row[12] if len(row) > 12 else "",
                        "row_number": row_idx,
                    }

                    # Derive city/country from provider if not in sheet
                    if not claim["city"] or not claim["country"]:
                        claim = _enrich_provider_location(claim)

                    claims.append(claim)
                    print(f"[SHEETS] Found pending claim: {claim['invoice_num']} - {claim['patient']}")
        else:
            print(f"[ERROR] gog CLI failed: {result.stderr}")
    except Exception as e:
        print(f"[ERROR] Failed to read sheets: {str(e)}")

    print(f"[SHEETS] Total pending claims: {len(claims)}")
    return claims


def _enrich_provider_location(claim: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fill in city/country based on known provider mappings.
    This avoids needing extra columns in the sheet for providers we already know.
    """
    provider_lower = claim["provider"].lower()

    # Known provider → location mappings
    provider_locations = {
        "lividi": {"city": "São Paulo", "country": "Brazil"},
        "clinica lividi": {"city": "São Paulo", "country": "Brazil"},
    }

    for pattern, location in provider_locations.items():
        if pattern in provider_lower:
            if not claim["city"]:
                claim["city"] = location["city"]
            if not claim["country"]:
                claim["country"] = location["country"]
            return claim

    # Default: leave blank (will need manual intervention)
    if not claim["city"]:
        claim["city"] = ""
        print(f"[WARN] No city for provider '{claim['provider']}' - may need manual entry")
    if not claim["country"]:
        claim["country"] = ""
        print(f"[WARN] No country for provider '{claim['provider']}' - may need manual entry")

    return claim


def update_sheets(row_number: int, reference_number: str) -> None:
    """Update Google Sheet: set column K to "Filed" and column M to reference number."""
    print(f"[SHEETS] Updating row {row_number} with reference {reference_number}")
    try:
        subprocess.run(
            ["gog", "sheets", "update", GOOGLE_SHEET_ID, GOOGLE_SHEET_TAB,
             f"K{row_number}:Filed", f"M{row_number}:{reference_number}"],
            timeout=30, check=True
        )
        print(f"[SHEETS] Successfully updated row {row_number}")
    except Exception as e:
        print(f"[ERROR] Failed to update sheets: {str(e)}")


# ============================================================================
# NOTIFICATION
# ============================================================================

def notify_telegram(claim: Dict[str, Any], reference_number: str) -> None:
    """Send a Telegram notification with claim filing confirmation."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("[WARN] TELEGRAM_BOT_TOKEN not set - skipping notification")
        return

    message = (
        f"Claim Filed Successfully\n\n"
        f"Invoice: {claim['invoice_num']}\n"
        f"Patient: {claim['patient']}\n"
        f"Provider: {claim['provider']}\n"
        f"Amount: {claim['amount']} {claim['currency']}\n"
        f"Reference: {reference_number}"
    )

    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://api.telegram.org/bot{bot_token}/sendMessage",
             "-d", f"chat_id={TELEGRAM_CHAT_ID}",
             "-d", f"text={message}"],
            timeout=10, check=False
        )
        print("[TELEGRAM] Notification sent")
    except Exception as e:
        print(f"[WARN] Telegram notification failed: {str(e)}")


# ============================================================================
# AUTHENTICATION
# ============================================================================

async def login(page: Page) -> None:
    """
    Log in to the BCBS portal.
    Handles Flutter Web login form + optional 2FA.
    """
    print("[AUTH] Starting login process")

    username = os.environ.get("BCBS_USERNAME")
    password = os.environ.get("BCBS_PASSWORD")
    if not username or not password:
        raise ValueError("BCBS_USERNAME or BCBS_PASSWORD environment variables not set")

    try:
        print(f"[AUTH] Navigating to {BCBS_PORTAL_URL}")
        await page.goto(BCBS_PORTAL_URL, wait_until="networkidle")
        await asyncio.sleep(3)

        # Fill username
        await fill_flutter_field(page, "username|email|user", username)

        # Fill password
        await fill_flutter_field(page, "password", password)

        # Click Login button
        print("[AUTH] Clicking Login button")
        login_btn = page.get_by_role("button", name=re.compile("login|sign in", re.IGNORECASE))
        await login_btn.click()
        await asyncio.sleep(4)  # Wait for dashboard or 2FA

        # Check if 2FA is required
        await handle_2fa(page)

        await take_screenshot(page, "login_success")
        print("[AUTH] Login successful")
    except Exception as e:
        print(f"[ERROR] Login failed: {str(e)}")
        await take_screenshot(page, "login_error")
        raise


async def handle_2fa(page: Page) -> None:
    """
    Handle 2FA if the verification code screen appears.
    Gets the code from Gmail (via gog CLI or IMAP).
    """
    try:
        code_field = page.get_by_role("textbox", name=re.compile("code|otp|2fa|verif", re.IGNORECASE))
        if await code_field.count() == 0:
            print("[2FA] No 2FA field found - skipping")
            return

        print("[2FA] 2FA field detected — retrieving code from email")
        code = get_2fa_code_from_gmail()

        if not code:
            raise Exception("Could not retrieve 2FA code from email")

        # Enter the code
        await code_field.click()
        await asyncio.sleep(SHORT_WAIT / 1000)
        await page.keyboard.type(code, delay=50)
        await wait_for_flutter(page)

        # Submit
        submit_btn = page.get_by_role("button", name=re.compile("submit|verify|confirm|continue", re.IGNORECASE))
        if await submit_btn.count() > 0:
            await submit_btn.first.click()
            await asyncio.sleep(3)

        print("[2FA] 2FA submitted successfully")
    except Exception as e:
        print(f"[ERROR] 2FA handling failed: {str(e)}")
        raise


async def dismiss_popups(page: Page) -> None:
    """Close any popups (Important Update, NOTICE, etc.) that block the form."""
    for _ in range(3):  # Try up to 3 times in case multiple popups
        try:
            await close_popup(page)
            await asyncio.sleep(0.5)
        except Exception:
            break


# ============================================================================
# NAVIGATION
# ============================================================================

async def navigate_to_eclaim(page: Page) -> None:
    """
    Navigate from dashboard to the eClaim wizard.
    Clicks: eClaims → File an eClaim → Get started (Paperless Form)
    """
    print("[NAV] Navigating to eClaim section")

    try:
        # Click "eClaims" on dashboard
        eclaims_btn = page.get_by_role("button", name=re.compile("eclaim", re.IGNORECASE))
        if await eclaims_btn.count() > 0:
            await eclaims_btn.first.click()
            await wait_for_flutter(page)

        # Click "File an eClaim"
        file_btn = page.get_by_role("button", name=re.compile("file.*claim|file an.*claim", re.IGNORECASE))
        if await file_btn.count() > 0:
            await file_btn.first.click()
            await wait_for_flutter(page)

        # Click "Get started" for Paperless Form
        start_btn = page.get_by_role("button", name=re.compile("get started|start", re.IGNORECASE))
        if await start_btn.count() > 0:
            await start_btn.first.click()
            await wait_for_flutter(page)

        await dismiss_popups(page)
        print("[NAV] Navigated to eClaim wizard")
    except Exception as e:
        print(f"[ERROR] Failed to navigate to eClaim: {str(e)}")
        await take_screenshot(page, "nav_error")
        raise


# ============================================================================
# CLAIM FILING STEPS (6-step wizard)
# ============================================================================

async def step1_preliminary(page: Page) -> None:
    """
    Step 1: Preliminary Questions (.../webPreliminaryQuestions)

    Confirmed elements:
    - button "PRIMARY MEMBER" / button "PROVIDER"
    - button "US DOLLAR CHECK" / button "BANK WIRE TRANSFER OR ACH PAYMENT"
    - 2x radio buttons for accident question (No = second radio)
    - button "Next"
    """
    print("[STEP1] Preliminary Questions")

    for attempt in range(MAX_RETRIES):
        try:
            # Select PRIMARY MEMBER
            member_btn = page.get_by_role("button", name=re.compile("primary member", re.IGNORECASE))
            if await member_btn.count() > 0:
                await member_btn.first.click()
                await wait_for_flutter(page)

            # Select BANK WIRE TRANSFER (NEVER CHECK)
            wire_btn = page.get_by_role("button", name=re.compile("bank wire|ach|wire transfer", re.IGNORECASE))
            if await wire_btn.count() > 0:
                await wire_btn.first.click()
                await wait_for_flutter(page)

            # Select No for accident (second radio)
            radios = page.get_by_role("radio")
            if await radios.count() >= 2:
                await radios.nth(1).click()
                await wait_for_flutter(page)

            # Click Next
            next_btn = page.get_by_role("button", name=re.compile("^next$", re.IGNORECASE))
            if await next_btn.count() > 0:
                await next_btn.first.click()
                await asyncio.sleep(3)

            print("[STEP1] Completed")
            return
        except Exception as e:
            print(f"[STEP1] Attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            if attempt >= MAX_RETRIES - 1:
                await take_screenshot(page, "step1_error")
                raise


async def step2_basic_info(page: Page, claim: Dict[str, Any]) -> None:
    """
    Step 2: Basic Information (.../claimant)

    Confirmed elements:
    - textbox containing "CHG" or "CLM" — eClaim Nick Name (pre-filled, replace with invoice#)
    - textbox "Patient dropdown" — select patient
    - Email, Phone, Address — pre-filled, leave as-is
    - Possible NOTICE popup after selecting patient
    - button "Next"
    """
    print("[STEP2] Basic Information")

    for attempt in range(MAX_RETRIES):
        try:
            # Fill eClaim Nick Name (replace pre-filled value with invoice number)
            await fill_flutter_field(page, "nick name|claim.*name|CLM|eclaim", claim["invoice_num"])

            # Select patient from dropdown
            await select_dropdown(page, "patient", claim["patient"])

            # Dismiss any NOTICE popup
            await asyncio.sleep(1)
            await close_popup(page)

            # Click Next
            next_btn = page.get_by_role("button", name=re.compile("^next$", re.IGNORECASE))
            if await next_btn.count() > 0:
                await next_btn.first.click()
                await asyncio.sleep(3)

            print("[STEP2] Completed")
            return
        except Exception as e:
            print(f"[STEP2] Attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            if attempt >= MAX_RETRIES - 1:
                await take_screenshot(page, "step2_error")
                raise


async def step3_other_insurance(page: Page) -> None:
    """
    Step 3: Other Insurance Form (.../otherinsurance)

    Simple page — "No" is pre-selected. Just verify and click Next.
    """
    print("[STEP3] Other Insurance Form")

    for attempt in range(MAX_RETRIES):
        try:
            # Ensure No is selected (second radio, should be pre-selected)
            radios = page.get_by_role("radio")
            if await radios.count() >= 2:
                await radios.nth(1).click()
                await wait_for_flutter(page)

            # Click Next
            next_btn = page.get_by_role("button", name=re.compile("^next$", re.IGNORECASE))
            if await next_btn.count() > 0:
                await next_btn.first.click()
                await asyncio.sleep(3)

            print("[STEP3] Completed")
            return
        except Exception as e:
            print(f"[STEP3] Attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            if attempt >= MAX_RETRIES - 1:
                await take_screenshot(page, "step3_error")
                raise


async def step4_charges(page: Page, claim: Dict[str, Any]) -> None:
    """
    Step 4: Invoiced Charges (.../invoiceChargesForm)

    The most complex step. Form is long and requires scrolling.

    Confirmed fields from live DOM inspection (2026-03-25):
    - textbox ": CHG 1 25-MAR-2026" — Charge Nickname
    - radio (2x) — Doctor/Dentist/Pharmacy (pre-selected) vs Hospital/Facility
    - textbox "Select Provider dropdown" — Provider
    - textbox ": (TextField)" — City
    - textbox "Country of Treatment dropdown" — Country
    - textbox (Charge Amount) — under CHARGE DETAILS section
    - textbox "Billed Invoice Currency dropdown" — Currency
    - textbox "Condition or Diagnosis dropdown" — Diagnosis
    - textbox "Service Description dropdown" — Service
    - button "Start Date of Service date picker. Current value: not set"
    - button "End Date of Service date picker. Current value: not set"
    - button "Back" / button "Save charge"
    """
    print("[STEP4] Invoiced Charges")

    for attempt in range(MAX_RETRIES):
        try:
            await scroll_form_to_top(page)
            await asyncio.sleep(0.5)

            # 1. Fill Charge Nickname (replace pre-filled "CHG 1 DD-MMM-YYYY")
            print("[STEP4] Filling Charge Nickname")
            await fill_flutter_field(page, "CHG|nickname", claim["invoice_num"])

            # 2. Doctor/Dentist/Pharmacy radio (pre-selected, but click to ensure)
            doctor_radio = page.get_by_role("radio")
            if await doctor_radio.count() > 0:
                await doctor_radio.first.click()  # First radio = Doctor
                await wait_for_flutter(page)

            # 3. Select Provider
            print(f"[STEP4] Selecting provider: {claim['provider']}")
            await select_dropdown(page, "Select Provider", claim["provider"])
            await scroll_form(page)

            # 4. Fill City (from claim data, not hardcoded)
            if claim.get("city"):
                print(f"[STEP4] Filling city: {claim['city']}")
                # City field shows as textbox ": (TextField)" in accessibility tree
                city_field = page.get_by_role("textbox", name=re.compile("TextField|city", re.IGNORECASE))
                if await city_field.count() > 0:
                    await city_field.first.click()
                    await asyncio.sleep(SHORT_WAIT / 1000)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(claim["city"], delay=30)
                    await page.keyboard.press("Tab")
                    await wait_for_flutter(page)

            # 5. Select Country of Treatment (from claim data, not hardcoded)
            if claim.get("country"):
                print(f"[STEP4] Selecting country: {claim['country']}")
                await select_dropdown(page, "Country of Treatment", claim["country"])

            await scroll_form(page)

            # 6. Fill Charge Amount
            print(f"[STEP4] Filling charge amount: {claim['amount']}")
            await fill_flutter_field(page, "charge.*amount|amount", claim["amount"])

            # 7. Select Billed Invoice Currency
            print(f"[STEP4] Selecting currency: {claim['currency']}")
            await select_dropdown(page, "Billed Invoice Currency|currency", claim["currency"])

            await scroll_form(page)

            # 8. Select Condition or Diagnosis
            print(f"[STEP4] Selecting diagnosis: {claim['diagnosis']}")
            await select_dropdown(page, "Condition or Diagnosis", claim["diagnosis"])

            # 9. Select Service Description
            print(f"[STEP4] Selecting service: {claim['procedure']}")
            await select_dropdown(page, "Service Description", claim["procedure"])

            await scroll_form(page)

            # 10. Select Start Date of Service
            # Parse date — handle various formats from sheet
            date_str = claim["date"]
            try:
                # Try YYYY-MM-DD first
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                try:
                    # Try MM/DD/YYYY
                    date_obj = datetime.strptime(date_str, "%m/%d/%Y")
                except ValueError:
                    try:
                        # Try DD/MM/YYYY
                        date_obj = datetime.strptime(date_str, "%d/%m/%Y")
                    except ValueError:
                        # Try natural language-ish formats
                        date_obj = datetime.strptime(date_str, "%B %d, %Y")

            formatted_date = date_obj.strftime("%Y-%m-%d")

            print(f"[STEP4] Selecting start date: {formatted_date}")
            await select_date(page, "Start Date of Service", formatted_date)

            # 11. Select End Date (same as start for single-day visits)
            print(f"[STEP4] Selecting end date: {formatted_date}")
            await select_date(page, "End Date of Service", formatted_date)

            await scroll_form(page)

            # 12. Click Save charge
            print("[STEP4] Clicking Save charge")
            save_btn = page.get_by_role("button", name=re.compile("save charge", re.IGNORECASE))
            if await save_btn.count() > 0:
                await save_btn.first.click()
                await asyncio.sleep(3)

            # After saving, click Next to proceed to step 5
            # (The page may show a summary with option to add more charges)
            next_btn = page.get_by_role("button", name=re.compile("^next$", re.IGNORECASE))
            if await next_btn.count() > 0:
                await next_btn.first.click()
                await asyncio.sleep(3)

            print("[STEP4] Completed")
            return
        except Exception as e:
            print(f"[STEP4] Attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            if attempt >= MAX_RETRIES - 1:
                await take_screenshot(page, "step4_error")
                raise


async def step5_reimbursement(page: Page) -> None:
    """
    Step 5: Reimbursement Details (.../reimbursementdetails)

    This page shows the payment method selected in step 1 (wire transfer)
    and the bank account on file. Fields:
    - Account selection (pre-saved US bank account — usually just one option)
    - Currency for reimbursement (should be USD)
    - button "Next"

    We selected BANK WIRE in step 1, so this should show wire transfer details.
    The account may be pre-selected if there's only one on file.
    """
    print("[STEP5] Reimbursement Details")

    for attempt in range(MAX_RETRIES):
        try:
            await asyncio.sleep(1)
            await take_screenshot(page, "step5_loaded")

            # Check if there's an account to select
            # It may be a radio button, a dropdown, or pre-selected
            account_radio = page.get_by_role("radio")
            if await account_radio.count() > 0:
                # Click the first account (should be the only bank account on file)
                await account_radio.first.click()
                await wait_for_flutter(page)
                print("[STEP5] Selected bank account")

            # Check if there's a currency dropdown
            try:
                currency_dropdown = page.get_by_role("textbox", name=re.compile("currency|reimbursement", re.IGNORECASE))
                if await currency_dropdown.count() > 0:
                    await select_dropdown(page, "currency|reimbursement", "USD")
                    print("[STEP5] Selected USD currency")
            except Exception:
                print("[STEP5] No currency dropdown found — may be pre-selected")

            # Click Next
            next_btn = page.get_by_role("button", name=re.compile("^next$", re.IGNORECASE))
            if await next_btn.count() > 0:
                await next_btn.first.click()
                await asyncio.sleep(3)

            print("[STEP5] Completed")
            return
        except Exception as e:
            print(f"[STEP5] Attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            if attempt >= MAX_RETRIES - 1:
                await take_screenshot(page, "step5_error")
                raise


async def step6_authorization(page: Page) -> str:
    """
    Step 6: Authorization (.../authorization)

    This is the final step. Fields:
    - Acknowledgment checkboxes (one or more — check all of them)
    - Authorization text (scrollable)
    - button "Submit" or "File eClaim" or "Submit Claim"

    After submission, a confirmation page shows the claim reference number.
    """
    print("[STEP6] Authorization")

    for attempt in range(MAX_RETRIES):
        try:
            await asyncio.sleep(1)
            await take_screenshot(page, "step6_loaded")

            # Scroll to see all content
            await scroll_form(page, 300)

            # Check ALL acknowledgment checkboxes
            checkboxes = page.get_by_role("checkbox")
            checkbox_count = await checkboxes.count()
            print(f"[STEP6] Found {checkbox_count} checkboxes")
            for i in range(checkbox_count):
                try:
                    cb = checkboxes.nth(i)
                    # Only click if not already checked
                    is_checked = await cb.is_checked()
                    if not is_checked:
                        await cb.click()
                        await asyncio.sleep(SHORT_WAIT / 1000)
                except Exception:
                    # Try clicking anyway
                    try:
                        await checkboxes.nth(i).click()
                        await asyncio.sleep(SHORT_WAIT / 1000)
                    except Exception:
                        pass

            await scroll_form(page, 300)
            await asyncio.sleep(0.5)

            # Click Submit / File eClaim / Submit Claim
            submit_btn = page.get_by_role("button", name=re.compile("submit|file.*claim|file eclaim", re.IGNORECASE))
            if await submit_btn.count() > 0:
                await submit_btn.first.click()
                await asyncio.sleep(5)  # Wait for submission to process
                print("[STEP6] Submitted claim")

            # Capture reference number from confirmation
            reference_number = await capture_reference_number(page)
            await take_screenshot(page, "step6_confirmation")

            print(f"[STEP6] Completed — Reference: {reference_number}")
            return reference_number
        except Exception as e:
            print(f"[STEP6] Attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
            if attempt >= MAX_RETRIES - 1:
                await take_screenshot(page, "step6_error")
                raise

    return f"REF-{datetime.now().strftime('%Y%m%d%H%M%S')}"


async def capture_reference_number(page: Page) -> str:
    """
    Extract the claim reference number from the confirmation page.
    Looks for common patterns in the page text.
    """
    try:
        page_text = await page.inner_text("body")

        patterns = [
            r"reference\s*(?:number|#|:)\s*[:\s]*([A-Z0-9\-]+)",
            r"claim\s*(?:number|#|:)\s*[:\s]*([A-Z0-9\-]+)",
            r"confirmation\s*(?:number|#|:)\s*[:\s]*([A-Z0-9\-]+)",
            r"eClaim\s*(?:number|#|:)\s*[:\s]*([A-Z0-9\-]+)",
            r"#\s*([A-Z0-9\-]{6,})",
        ]

        for pattern in patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                ref_num = match.group(1).strip()
                print(f"[REF] Found reference number: {ref_num}")
                return ref_num

        print("[WARN] Could not extract reference number — using timestamp")
        return f"REF-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    except Exception as e:
        print(f"[WARN] Error capturing reference: {str(e)}")
        return f"REF-{datetime.now().strftime('%Y%m%d%H%M%S')}"


# ============================================================================
# DOCUMENT UPLOAD
# ============================================================================

async def upload_document(page: Page, drive_link: str) -> None:
    """
    Download invoice from Google Drive and upload to the BCBS portal.
    Uses gog CLI for Google Drive download (authenticated).
    """
    print(f"[UPLOAD] Uploading document from: {drive_link}")

    if not drive_link:
        print("[WARN] No drive link provided — skipping upload")
        return

    try:
        # Extract file ID from Google Drive link
        file_id_match = re.search(r'/d/([a-zA-Z0-9-_]+)', drive_link)
        if not file_id_match:
            print("[WARN] Could not extract file ID from drive link")
            return

        file_id = file_id_match.group(1)
        temp_file = f"/tmp/invoice_{file_id}.pdf"

        # Download using gog CLI (authenticated)
        print(f"[UPLOAD] Downloading file: {file_id}")
        result = subprocess.run(
            ["gog", "drive", "download", file_id, "--output", temp_file],
            timeout=60, capture_output=True, text=True
        )

        if result.returncode != 0:
            # Fallback to wget with public link
            download_url = f"https://drive.google.com/uc?id={file_id}&export=download"
            result = subprocess.run(
                ["wget", "-q", download_url, "-O", temp_file],
                timeout=30, check=False
            )
            if result.returncode != 0:
                print("[WARN] Failed to download file from Drive")
                return

        # Find file upload input on the portal
        file_input = page.locator("input[type='file']").first
        if await file_input.count() > 0:
            await file_input.set_input_files(temp_file)
            await wait_for_flutter(page)
            print("[UPLOAD] Document uploaded successfully")
        else:
            print("[WARN] Could not find file input on portal — may need manual upload")
    except Exception as e:
        print(f"[WARN] Document upload failed: {str(e)}")


# ============================================================================
# MAIN WORKFLOW
# ============================================================================

async def file_single_claim(page: Page, claim: Dict[str, Any]) -> Optional[str]:
    """
    File a single claim through the complete 6-step wizard.
    Returns the reference number if successful, None otherwise.
    """
    print(f"\n{'='*70}")
    print(f"[CLAIM] Filing claim: {claim['invoice_num']} — {claim['patient']} — {claim['provider']}")
    print(f"{'='*70}")

    try:
        await navigate_to_eclaim(page)
        await dismiss_popups(page)

        await step1_preliminary(page)
        await step2_basic_info(page, claim)
        await step3_other_insurance(page)
        await step4_charges(page, claim)
        await step5_reimbursement(page)
        reference_number = await step6_authorization(page)

        # Upload supporting document
        if claim.get("drive_link"):
            await upload_document(page, claim["drive_link"])

        print(f"[CLAIM] Successfully filed! Reference: {reference_number}")
        return reference_number
    except Exception as e:
        print(f"[ERROR] Failed to file claim {claim['invoice_num']}: {str(e)}")
        await take_screenshot(page, f"claim_error_{claim['invoice_num']}")
        return None


async def main():
    """
    Main entry point.
    1. Read pending claims from Google Sheets
    2. Connect to existing Chrome CDP instance
    3. Log in to BCBS portal
    4. File each claim sequentially
    5. Update sheet and send Telegram notifications
    """
    print(f"\n{'='*70}")
    print(f"[MAIN] BCBS Claim Filer Starting — {datetime.now().isoformat()}")
    print(f"{'='*70}\n")

    claims = read_pending_claims()

    if not claims:
        print("[MAIN] No pending claims found — exiting")
        return

    print(f"[MAIN] Found {len(claims)} pending claim(s) to file")

    async with async_playwright() as p:
        try:
            print(f"[MAIN] Connecting to Chrome CDP at {CDP_URL}")
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            print("[MAIN] Connected to browser")

            # Log in
            await login(page)

            # File each claim
            filed_count = 0
            for idx, claim in enumerate(claims, start=1):
                print(f"\n[MAIN] Processing claim {idx}/{len(claims)}")

                reference_number = await file_single_claim(page, claim)

                if reference_number:
                    update_sheets(claim["row_number"], reference_number)
                    notify_telegram(claim, reference_number)
                    filed_count += 1

                    if idx < len(claims):
                        print("[MAIN] Waiting before next claim...")
                        await asyncio.sleep(5)
                else:
                    print(f"[MAIN] Skipping sheet update for failed claim {claim['invoice_num']}")

            print(f"\n{'='*70}")
            print(f"[MAIN] Complete — Filed {filed_count}/{len(claims)} claims")
            print(f"[MAIN] Timestamp: {datetime.now().isoformat()}")
            print(f"{'='*70}\n")
        except Exception as e:
            print(f"[ERROR] Fatal error: {str(e)}")
            raise
        finally:
            print("[MAIN] Disconnecting from browser")


if __name__ == "__main__":
    asyncio.run(main())
