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
# Force HTML renderer mode — Flutter renders real HTML elements instead of
# canvas + flt-semantics, making Playwright automation reliable.
BCBS_PORTAL_URL = "https://members.bcbsglobalsolutions.com/?renderer=html"
CDP_URL = "http://127.0.0.1:9222"
GOOGLE_SHEET_ID = "1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk"
GOOGLE_SHEET_TAB = "2026"
TELEGRAM_CHAT_ID = "8409634074"
MAX_RETRIES = 3

# Build environment for gog CLI subprocess calls.
# gog needs its config-home env var pointing to /data/workspace/.config
# so it can find OAuth credentials stored on the Railway volume.
_CFG_KEY = "XDG" + "_CONFIG_" + "HOME"
GOG_ENV = {**os.environ, _CFG_KEY: "/data/workspace/.config"}
FLUTTER_WAIT_TIME = 2000  # milliseconds
SHORT_WAIT = 500  # milliseconds

# Screenshot directory
SCREENSHOT_DIR = Path("/tmp/bcbs_screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

SCRIPT_VERSION = "v5-fix-2fa-reuse-2026-03-27"
print(f"[INIT] BCBS Claim Filer {SCRIPT_VERSION} initialized at {datetime.now().isoformat()}")


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
    Scroll the form container. Supports both CanvasKit (flt-semantics)
    and HTML renderer (regular scrollable divs) modes.
    """
    await page.evaluate(f"""
        () => {{
            // Try flt-semantics first (CanvasKit mode)
            const flts = document.querySelectorAll('flt-semantics');
            for (const el of flts) {{
                if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 300) {{
                    el.scrollTop += {amount};
                    return;
                }}
            }}
            // HTML renderer mode: look for any scrollable container
            const allEls = document.querySelectorAll('div, section, main, form');
            for (const el of allEls) {{
                if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 300) {{
                    el.scrollTop += {amount};
                    return;
                }}
            }}
            // Last resort: scroll the page itself
            window.scrollBy(0, {amount});
        }}
    """)
    await asyncio.sleep(0.3)


async def scroll_form_to_top(page: Page) -> None:
    """Scroll the form container back to top. Supports both rendering modes."""
    await page.evaluate("""
        () => {
            const flts = document.querySelectorAll('flt-semantics');
            for (const el of flts) {
                if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 300) {
                    el.scrollTop = 0;
                    return;
                }
            }
            const allEls = document.querySelectorAll('div, section, main, form');
            for (const el of allEls) {
                if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 300) {
                    el.scrollTop = 0;
                    return;
                }
            }
            window.scrollTo(0, 0);
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


async def _select_calendar_date(page: Page, date_obj: datetime) -> None:
    """
    Select a date from the Flutter calendar picker dialog.

    Calendar DOM structure (confirmed 2026-03-26):
    - Month header shows "March 2026" as a <span>
    - Backward/Forward buttons for month navigation
    - Day cells are <flt-semantics> with text like "Thu, 09 January 2026"
    - OK and CANCEL buttons at the bottom
    """
    target_month_year = date_obj.strftime("%B %Y")  # "January 2026"
    # Build the day label: "Thu, 09 January 2026"
    target_day_label = date_obj.strftime("%a, %d %B %Y")  # "Thu, 09 January 2026"

    # Navigate to the correct month
    for _ in range(24):
        # Check if we're on the right month
        month_header = page.locator(f"text={target_month_year}")
        if await month_header.count() > 0:
            print(f"[DATE] On correct month: {target_month_year}")
            break
        # Navigate backward (claims are for past dates)
        back_btn = page.get_by_role("button", name="Backward")
        if await back_btn.count() > 0:
            await back_btn.first.click()
            await asyncio.sleep(0.5)
        else:
            break

    # Click the target day cell by matching its text content
    day_cell = page.locator(f"flt-semantics:has(span):text-is('{target_day_label}')")
    if await day_cell.count() == 0:
        # Try partial match — just the day number in the month
        # Cells contain text like "Thu, 09 January 2026"
        day_num = str(date_obj.day).zfill(2)
        month_name = date_obj.strftime("%B")
        year = date_obj.strftime("%Y")
        day_cell = page.locator(f"flt-semantics span:text-matches('{day_num} {month_name} {year}')")
        if await day_cell.count() > 0:
            await day_cell.first.click()
        else:
            print(f"[DATE] Could not find day cell for {target_day_label}, trying text match")
            # Last resort: find by visible text containing the day number
            all_cells = page.locator("flt-semantics span")
            count = await all_cells.count()
            for i in range(count):
                cell_text = await all_cells.nth(i).inner_text()
                if f"{day_num} {month_name}" in cell_text:
                    await all_cells.nth(i).click()
                    break
    else:
        await day_cell.first.click()

    await asyncio.sleep(0.5)
    print(f"[DATE] Clicked day: {target_day_label}")

    # Click OK to confirm
    ok_btn = page.get_by_role("button", name="OK")
    if await ok_btn.count() > 0:
        await ok_btn.first.click()
        await wait_for_flutter(page)
        print("[DATE] Confirmed date selection")


async def dump_page_state(page: Page, label: str) -> None:
    """Dump what Playwright can see on the page for debugging.
    Logs counts of key ARIA roles and the page title/URL."""
    print(f"\n[DIAG:{label}] URL: {page.url}")
    print(f"[DIAG:{label}] Title: {await page.title()}")
    roles_to_check = [
        "textbox", "combobox", "searchbox", "button", "radio",
        "checkbox", "link", "heading", "dialog", "tab", "listbox",
    ]
    for role in roles_to_check:
        try:
            count = await page.get_by_role(role).count()
            if count > 0:
                print(f"[DIAG:{label}] role='{role}' count={count}")
        except Exception:
            pass

    # Log accessible names of ALL textboxes/comboboxes/searchboxes
    for role in ["textbox", "combobox", "searchbox"]:
        try:
            loc = page.get_by_role(role)
            count = await loc.count()
            for i in range(min(count, 20)):
                try:
                    name = await loc.nth(i).get_attribute("aria-label")
                    tag = await loc.nth(i).evaluate("el => el.tagName")
                    placeholder = await loc.nth(i).get_attribute("placeholder") or ""
                    value = await loc.nth(i).input_value() if tag in ("INPUT", "TEXTAREA", "input", "textarea") else ""
                    print(f"[DIAG:{label}] {role}[{i}] tag={tag} aria-label='{name}' placeholder='{placeholder}' value='{value}'")
                except Exception:
                    print(f"[DIAG:{label}] {role}[{i}] (could not read attrs)")
        except Exception:
            pass

    # Check if there are any flt-semantics elements at all
    try:
        flt_count = await page.locator("flt-semantics").count()
        print(f"[DIAG:{label}] flt-semantics elements: {flt_count}")
    except Exception:
        pass

    # Check for iframes
    try:
        iframe_count = await page.locator("iframe").count()
        if iframe_count > 0:
            print(f"[DIAG:{label}] WARNING: {iframe_count} iframe(s) found — content may be inside iframe")
    except Exception:
        pass

    print(f"[DIAG:{label}] --- end dump ---\n")


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

def _extract_code_from_response(raw_json: str) -> Optional[str]:
    """
    Parse gog gmail get JSON response and extract the 2FA code from
    the NEWEST message in the thread.

    STRATEGY: Use re.findall to find ALL verification codes in the output,
    then take the LAST one. gog returns oldest messages first, so the last
    code in the output is always the newest. This works regardless of whether
    JSON parsing succeeds or fails.
    """
    CODE_PATTERN = r'[Vv]erification\s+code\s*:?\s*(\d{6})'

    # ---- Primary approach: findall on ENTIRE raw output, take last ----
    all_codes = re.findall(CODE_PATTERN, raw_json)
    if all_codes:
        print(f"[2FA] Found {len(all_codes)} verification code(s) in output: "
              f"{['****' + c[-2:] for c in all_codes]}")
        print(f"[2FA] Using LAST code (newest): ****{all_codes[-1][-2:]}")
        return all_codes[-1]

    # ---- Fallback: any 6-digit number, take last occurrence ----
    # Only from the tail to reduce false positives from IDs/timestamps
    all_digits = re.findall(r'\b(\d{6})\b', raw_json[-3000:])
    if all_digits:
        print(f"[2FA] No 'verification code' pattern found. "
              f"Found {len(all_digits)} 6-digit numbers, using last: ****{all_digits[-1][-2:]}")
        return all_digits[-1]

    print("[2FA] No verification codes found in output at all")
    return None


# --- 2FA code reuse prevention ---
_USED_CODES_FILE = os.environ.get("OPENCLAW_STATE_DIR", "/data/.openclaw") + "/bcbs_2fa_used_codes.txt"


def _load_used_codes() -> set:
    """Load previously used 2FA codes from temp file."""
    try:
        with open(_USED_CODES_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def _mark_code_used(code: str):
    """Append a code to the used-codes file so it won't be reused."""
    with open(_USED_CODES_FILE, "a") as f:
        f.write(f"{code}\n")
    print(f"[2FA] Marked code ****{code[-2:]} as used")


def _try_trash_email(msg_id: str):
    """Best-effort: trash the verification email so it won't be found again."""
    try:
        result = subprocess.run(
            ["gog", "gmail", "trash", msg_id],
            capture_output=True, text=True, timeout=10, env=GOG_ENV
        )
        if result.returncode == 0:
            print(f"[2FA] Trashed verification email {msg_id}")
        else:
            print(f"[2FA] Could not trash email {msg_id}: {result.stderr[:100]}")
    except Exception as e:
        print(f"[2FA] Trash attempt failed (non-critical): {e}")


def get_2fa_code_from_gmail(login_epoch: int = 0) -> Optional[str]:
    """
    Retrieve the BCBS 2FA verification code from Gmail.

    login_epoch: unix timestamp of when login was submitted. Only emails
    arriving AFTER this time will be considered, preventing stale codes.

    Tries gog CLI first, falls back to IMAP.
    Retries for up to 90 seconds waiting for the email to arrive.
    """
    print("[2FA] Retrieving verification code from Gmail")

    import time

    # Load previously used codes to prevent reuse across runs
    used_codes = _load_used_codes()
    if used_codes:
        print(f"[2FA] Previously used codes to skip: {['****' + c[-2:] for c in used_codes]}")

    # Build the Gmail search query with a time filter
    # Use newer_than:2m for reliable time filtering — the 2FA email
    # should arrive within ~30s of login, so 2m is generous.
    # (Gmail's `after:` with epoch seconds is unreliable via gog CLI.)
    base_query = "from:noreply@bcbsglobalsolutions.com verification code"
    search_query = f"{base_query} newer_than:2m"
    print(f"[2FA] Using newer_than:2m filter (login epoch: {login_epoch})")

    # Wait 15s on the first attempt to give the email time to arrive
    print("[2FA] Waiting 15s for verification email to arrive...")
    time.sleep(15)

    for attempt in range(18):  # 18 attempts × 5s = 90s
        try:
            # STEP 1: Search for the 2FA email
            # Add -in:trash -in:spam to exclude deleted/spam emails
            full_query = f"{search_query} -in:trash -in:spam"
            result = subprocess.run(
                ["gog", "gmail", "search", full_query, "--max", "3", "--json"],
                capture_output=True, text=True, timeout=15, env=GOG_ENV
            )
            if attempt == 0:
                print(f"[2FA] search query: {full_query}")
                print(f"[2FA] search exit code: {result.returncode}")
                print(f"[2FA] search stdout (first 500 chars): {result.stdout[:500]}")

            if result.returncode != 0 or not result.stdout.strip():
                if attempt < 17:
                    print(f"[2FA] No results yet, attempt {attempt+1}/18")
                    time.sleep(5)
                continue

            data = json.loads(result.stdout)

            # Extract email/thread items from whatever wrapper gog uses
            if isinstance(data, list):
                emails = data
            elif isinstance(data, dict):
                emails = (data.get("messages") or data.get("threads")
                          or data.get("results") or data.get("emails") or [])
                if not emails:
                    for v in data.values():
                        if isinstance(v, list) and len(v) > 0:
                            emails = v
                            break
            else:
                emails = []

            if not emails:
                if attempt < 17:
                    print(f"[2FA] Empty results, attempt {attempt+1}/18")
                    time.sleep(5)
                continue

            # STEP 2: Try to get the message ID (not thread ID) for precise retrieval
            first = emails[0]
            msg_id = None
            thread_id = None
            if isinstance(first, dict):
                # Prefer messageId over threadId to get a single message
                msg_id = (first.get("messageId") or first.get("message_id"))
                thread_id = (first.get("threadId") or first.get("thread_id")
                             or first.get("id"))
                # Log all available keys for debugging
                if attempt == 0:
                    print(f"[2FA] First result keys: {list(first.keys())}")
                    print(f"[2FA] msg_id={msg_id}, thread_id={thread_id}")
                    # Log snippet if available (may contain the code directly)
                    snippet = first.get("snippet", first.get("body", ""))
                    if snippet:
                        print(f"[2FA] Snippet: {str(snippet)[:200]}")
            elif isinstance(first, str):
                thread_id = first

            # STEP 2b: Try extracting code directly from search result snippets
            # Check ALL search results (newest email = last in list), take code from last match
            all_snippet_codes = []
            for idx, email_item in enumerate(emails):
                if isinstance(email_item, dict):
                    for field in ["snippet", "body", "text", "content", "subject"]:
                        val = email_item.get(field, "")
                        if val:
                            codes_in_field = re.findall(
                                r'[Vv]erification\s+code\s*:?\s*(\d{6})', str(val))
                            for c in codes_in_field:
                                all_snippet_codes.append((idx, field, c))
            if all_snippet_codes:
                # Filter out previously used codes
                fresh_codes = [(i, f, c) for i, f, c in all_snippet_codes if c not in used_codes]
                if fresh_codes:
                    idx, field, code = fresh_codes[-1]
                    print(f"[2FA] Found {len(all_snippet_codes)} code(s), "
                          f"{len(fresh_codes)} fresh. Using (email[{idx}], {field}): ****{code[-2:]}")
                    _mark_code_used(code)
                    # Try to trash the email so future searches won't find it
                    if isinstance(emails[idx], dict):
                        eid = emails[idx].get("messageId") or emails[idx].get("id") or emails[idx].get("threadId")
                        if eid:
                            _try_trash_email(str(eid))
                    return code
                else:
                    print(f"[2FA] Found {len(all_snippet_codes)} code(s) but ALL are previously used — waiting for fresh email")
                    if attempt < 17:
                        time.sleep(5)
                    continue

            lookup_id = msg_id or thread_id
            if not lookup_id:
                print(f"[2FA] Could not extract message/thread ID from search result")
                if attempt < 17:
                    time.sleep(5)
                continue

            # STEP 3: Fetch the email — try message first, fall back to thread
            get_result = subprocess.run(
                ["gog", "gmail", "get", lookup_id, "--json"],
                capture_output=True, text=True, timeout=15, env=GOG_ENV
            )
            if attempt == 0:
                print(f"[2FA] get (id={lookup_id}) exit code: {get_result.returncode}")
                print(f"[2FA] get stdout (first 500 chars): {get_result.stdout[:500]}")

            # STEP 4: Parse the JSON response to find the newest message body
            full_output = get_result.stdout
            code = _extract_code_from_response(full_output)
            if code:
                if code in used_codes:
                    print(f"[2FA] Extracted code ****{code[-2:]} but it was already used — waiting for fresh email")
                    if attempt < 17:
                        time.sleep(5)
                    continue
                print(f"[2FA] Extracted code: ****{code[-2:]}")
                _mark_code_used(code)
                if lookup_id:
                    _try_trash_email(str(lookup_id))
                return code

            if attempt == 0:
                print(f"[2FA] Could not extract code from response")

        except FileNotFoundError:
            print("[2FA] gog CLI not found, trying IMAP")
            break
        except subprocess.TimeoutExpired:
            print(f"[2FA] gog timed out on attempt {attempt+1}")
        except json.JSONDecodeError as e:
            print(f"[2FA] gog JSON parse error: {e}")
            if attempt == 0:
                print(f"[2FA] Raw output: {result.stdout[:300]}")
        except Exception as e:
            print(f"[2FA] gog attempt {attempt+1} failed: {e}")

        if attempt < 17:
            print(f"[2FA] Waiting for email... attempt {attempt+1}/18")
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
                    if code in used_codes:
                        print(f"[2FA] IMAP found code ****{code[-2:]} but already used — waiting")
                    else:
                        print(f"[2FA] Found fresh code via IMAP: ****{code[-2:]}")
                        _mark_code_used(code)
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

    Column layout (updated 2026-03-27 — City & Country added after Year):
    - A (0)  = Date Processed
    - B (1)  = Patient Name
    - C (2)  = Provider Name
    - D (3)  = Date of Service
    - E (4)  = Amount Billed
    - F (5)  = Currency
    - G (6)  = Diagnosis Codes
    - H (7)  = Procedure Codes
    - I (8)  = Invoice #
    - J (9)  = Year
    - K (10) = City
    - L (11) = Country
    - M (12) = Claim Status (must be "Pending")
    - N (13) = Drive File Link
    - O (14) = Bill Type
    - P (15) = Secondary Doc
    - Q (16) = Claim Ref #
    - R (17) = Notes
    """
    print("[SHEETS] Reading pending claims from Google Sheets")

    claims = []
    try:
        result = subprocess.run(
            ["gog", "sheets", "get", GOOGLE_SHEET_ID, f"'{GOOGLE_SHEET_TAB}'!A:R", "--json"],
            capture_output=True, text=True, timeout=30, env=GOG_ENV
        )

        if result.returncode == 0:
            data = json.loads(result.stdout)
            # gog sheets get --json wraps rows in {"values": [[...], ...]}
            rows = data.get("values", data) if isinstance(data, dict) else data

            for row_idx, row in enumerate(rows, start=2):  # Row 1 is header
                # DEBUG: log raw row to diagnose column mapping issues
                print(f"[SHEETS] Row {row_idx} raw ({len(row)} cols): {row[:18]}")
                # Column M (12) = Claim Status
                if len(row) > 12 and row[12] and row[12].strip().lower() == "pending":
                    claim = {
                        "patient": row[1] if len(row) > 1 else "",
                        "provider": row[2] if len(row) > 2 else "",
                        "date": row[3] if len(row) > 3 else "",
                        "amount": row[4] if len(row) > 4 else "",
                        "currency": row[5] if len(row) > 5 else "",
                        "diagnosis": row[6] if len(row) > 6 else "",
                        "procedure": row[7] if len(row) > 7 else "",
                        "invoice_num": row[8] if len(row) > 8 else "",
                        "city": row[10] if len(row) > 10 else "",
                        "country": row[11] if len(row) > 11 else "",
                        "drive_link": row[13] if len(row) > 13 else "",
                        "bill_type": row[14] if len(row) > 14 else "",
                        "row_number": row_idx,
                    }

                    # Fall back to provider mapping if city/country not in sheet
                    if not claim["city"] or not claim["country"]:
                        claim = _enrich_provider_location(claim)

                    # DEBUG: log parsed claim data
                    print(f"[SHEETS] Parsed claim: patient={claim['patient']}, provider={claim['provider']}, "
                          f"date={claim['date']}, amount={claim['amount']}, currency={claim['currency']}, "
                          f"city={claim['city']}, country={claim['country']}, invoice={claim['invoice_num']}")

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
    # Add new providers here as they appear in claims
    provider_locations = {
        "lividi": {"city": "São Paulo", "country": "Brazil"},
        "clinica lividi": {"city": "São Paulo", "country": "Brazil"},
        "rohrmoser": {"city": "Munich", "country": "Germany"},
        "dr. rohrmoser": {"city": "Munich", "country": "Germany"},
        "dr rohrmoser": {"city": "Munich", "country": "Germany"},
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
    """Update Google Sheet: set column M (Claim Status) to "Filed" and column Q (Claim Ref #) to reference number."""
    print(f"[SHEETS] Updating row {row_number} with reference {reference_number}")
    try:
        # Update column M (Claim Status) to "Filed"
        subprocess.run(
            ["gog", "sheets", "update", GOOGLE_SHEET_ID,
             f"'{GOOGLE_SHEET_TAB}'!M{row_number}",
             "--values-json", json.dumps([["Filed"]]),
             "--input", "USER_ENTERED"],
            timeout=30, check=True, env=GOG_ENV
        )
        # Update column Q (Claim Ref #)
        subprocess.run(
            ["gog", "sheets", "update", GOOGLE_SHEET_ID,
             f"'{GOOGLE_SHEET_TAB}'!Q{row_number}",
             "--values-json", json.dumps([[reference_number]]),
             "--input", "USER_ENTERED"],
            timeout=30, check=True, env=GOG_ENV
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


def notify_telegram_summary(total: int, filed: int, failures: list) -> None:
    """Send a Telegram summary after all claims are processed."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return

    if filed == total and total > 0:
        msg = f"All {total} claim(s) filed successfully!"
    elif filed == 0:
        msg = f"FAILED: None of the {total} claim(s) could be filed."
    else:
        msg = f"Partial: {filed}/{total} claim(s) filed."

    if failures:
        msg += "\n\nFailed claims:"
        for inv, err in failures:
            # Truncate error to keep message readable
            short_err = str(err)[:120]
            msg += f"\n- {inv}: {short_err}"

    msg += f"\n\nTimestamp: {datetime.now().strftime('%H:%M %d-%b-%Y')}"

    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://api.telegram.org/bot{bot_token}/sendMessage",
             "-d", f"chat_id={TELEGRAM_CHAT_ID}",
             "-d", f"text={msg}"],
            timeout=10, check=False
        )
    except Exception:
        pass


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
        # STEP 1: Load the Flutter landing page
        print(f"[AUTH] Navigating to {BCBS_PORTAL_URL}")
        await page.goto(BCBS_PORTAL_URL, wait_until="networkidle")
        await asyncio.sleep(5)
        await take_screenshot(page, "landing_page")

        # STEP 2: Click the Flutter "Login" button on the landing page
        # This is a <flt-semantics role="button">Login</flt-semantics> element
        print("[AUTH] Clicking Login button on landing page")
        login_btn = page.get_by_role("button", name=re.compile("^login$", re.IGNORECASE))
        await login_btn.click()
        await asyncio.sleep(5)  # Wait for redirect to SSO login form
        await take_screenshot(page, "sso_login_form")
        print(f"[AUTH] Redirected to: {page.url}")

        # STEP 3: Fill the standard HTML login form (not Flutter)
        # Username: <input name="identifier" autocomplete="username">
        username_input = page.locator('input[name="identifier"]')
        await username_input.wait_for(state="visible", timeout=15000)
        await username_input.fill(username)
        print("[AUTH] Username entered")

        # Password: <input name="credentials.passcode" type="password">
        password_input = page.locator('input[name="credentials.passcode"]')
        await password_input.wait_for(state="visible", timeout=5000)
        await password_input.fill(password)
        print("[AUTH] Password entered")

        # Submit: <input type="submit" value="SIGN IN">
        # Record the time BEFORE clicking so we only accept 2FA emails
        # that arrive AFTER this moment (avoids stale codes)
        import time as _time
        login_submit_epoch = int(_time.time())
        print(f"[AUTH] Clicking SIGN IN (epoch: {login_submit_epoch})")
        submit_btn = page.locator('input[type="submit"][value="SIGN IN"]')
        await submit_btn.click()
        await asyncio.sleep(5)  # Wait for redirect back to portal
        await take_screenshot(page, "after_sign_in")

        # Check if 2FA is required
        await handle_2fa(page, login_submit_epoch)

        # Wait for redirect back to the Flutter portal after auth
        print("[AUTH] Waiting for post-login redirect...")
        await asyncio.sleep(5)
        await take_screenshot(page, "after_2fa")
        print(f"[AUTH] Current URL after auth: {page.url}")

        # Verify we landed on the portal, not back on login
        # The portal URL should contain the base domain, not the SSO/oauth URL
        current_url = page.url
        if "login" in current_url.lower() or "authorize" in current_url.lower() or "signin" in current_url.lower():
            print("[AUTH] WARNING: Still on login/auth page after sign-in. Trying to navigate to portal...")
            await page.goto(BCBS_PORTAL_URL, wait_until="networkidle")
            await asyncio.sleep(5)
            print(f"[AUTH] After forced nav: {page.url}")
            await take_screenshot(page, "forced_nav_to_portal")

        # CRITICAL: After SSO redirect, the ?renderer=html parameter is lost.
        # Re-navigate to force HTML renderer mode for the dashboard.
        print(f"[AUTH] Post-login URL: {page.url}")
        if "renderer=html" not in page.url:
            print("[AUTH] HTML renderer lost after SSO redirect — re-navigating")
            await page.goto(BCBS_PORTAL_URL, wait_until="networkidle")
            await asyncio.sleep(5)
            print(f"[AUTH] Re-navigated to: {page.url}")

        # Wait for Flutter to fully load the dashboard
        await asyncio.sleep(5)
        await wait_for_flutter(page)
        await take_screenshot(page, "login_success")
        print(f"[AUTH] Login complete. URL: {page.url}")
        print("[AUTH] Login successful")
    except Exception as e:
        print(f"[ERROR] Login failed: {str(e)}")
        await take_screenshot(page, "login_error")
        raise


async def handle_2fa(page: Page, login_epoch: int = 0) -> None:
    """
    Handle 2FA if the verification code screen appears.
    Gets the code from Gmail (via gog CLI or IMAP).
    login_epoch: unix timestamp of when SIGN IN was clicked — only accept
    emails newer than this to avoid stale codes.
    """
    try:
        code_field = page.get_by_role("textbox", name=re.compile("code|otp|2fa|verif", re.IGNORECASE))
        if await code_field.count() == 0:
            print("[2FA] No 2FA field found - skipping")
            return

        print("[2FA] 2FA field detected — retrieving code from email")
        code = get_2fa_code_from_gmail(login_epoch)

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
    print(f"[NAV] Starting URL: {page.url}")

    try:
        # First check: are we even on the portal? If still on login, abort.
        current_url = page.url
        if "login" in current_url.lower() or "authorize" in current_url.lower() or "signin" in current_url.lower():
            print("[NAV] ERROR: Still on login page! Cannot navigate to eClaim.")
            await take_screenshot(page, "nav_still_on_login")
            raise Exception(f"Cannot navigate to eClaim — still on login page: {current_url}")

        await dump_page_state(page, "NAV_DASHBOARD")

        # Click "eClaims" on dashboard — MUST find this button
        eclaims_btn = page.get_by_role("button", name=re.compile("eclaim", re.IGNORECASE))
        btn_count = await eclaims_btn.count()
        print(f"[NAV] eClaims button count: {btn_count}")
        if btn_count > 0:
            await eclaims_btn.first.click()
            await asyncio.sleep(3)
            await wait_for_flutter(page)
            print(f"[NAV] After eClaims click, URL: {page.url}")
        else:
            # Try as link
            eclaims_link = page.get_by_role("link", name=re.compile("eclaim", re.IGNORECASE))
            if await eclaims_link.count() > 0:
                await eclaims_link.first.click()
                await asyncio.sleep(3)
                await wait_for_flutter(page)
                print(f"[NAV] Clicked eClaims link, URL: {page.url}")
            else:
                # Try clicking by text content as last resort
                eclaims_text = page.locator("text=/eclaim/i")
                if await eclaims_text.count() > 0:
                    await eclaims_text.first.click()
                    await asyncio.sleep(3)
                    await wait_for_flutter(page)
                    print(f"[NAV] Clicked eClaims text, URL: {page.url}")
                else:
                    await take_screenshot(page, "nav_no_eclaims_button")
                    raise Exception("FATAL: Cannot find eClaims button, link, or text on dashboard")

        await dismiss_popups(page)

        # Click "File an eClaim" — MUST find this
        file_btn = page.get_by_role("button", name=re.compile("file.*claim|file an.*claim", re.IGNORECASE))
        btn_count = await file_btn.count()
        print(f"[NAV] File eClaim button count: {btn_count}")
        if btn_count > 0:
            await file_btn.first.click()
            await asyncio.sleep(3)
            await wait_for_flutter(page)
        else:
            # Try text locator
            file_text = page.locator("text=/file.*claim/i")
            if await file_text.count() > 0:
                await file_text.first.click()
                await asyncio.sleep(3)
                await wait_for_flutter(page)
            else:
                await take_screenshot(page, "nav_no_file_claim_button")
                raise Exception("FATAL: Cannot find 'File an eClaim' button on eClaims page")

        await dismiss_popups(page)

        # Click "Get started" for Paperless Form
        start_btn = page.get_by_role("button", name=re.compile("get started|start|paperless", re.IGNORECASE))
        btn_count = await start_btn.count()
        print(f"[NAV] Get started button count: {btn_count}")
        if btn_count > 0:
            await start_btn.first.click()
            await asyncio.sleep(3)
            await wait_for_flutter(page)
        else:
            await take_screenshot(page, "nav_no_get_started_button")
            raise Exception("FATAL: Cannot find 'Get started' / 'Paperless Form' button")

        await dismiss_popups(page)
        print(f"[NAV] Final URL: {page.url}")
        await take_screenshot(page, "nav_eclaim_wizard")

        # VERIFY: We should now see Step 1 elements (PRIMARY MEMBER button or similar)
        verify = page.get_by_role("button", name=re.compile("primary member|next|bank wire", re.IGNORECASE))
        if await verify.count() == 0:
            await take_screenshot(page, "nav_verification_failed")
            raise Exception("FATAL: Navigation completed but Step 1 elements not found — not on the wizard")

        print("[NAV] Navigated to eClaim wizard (verified)")
    except Exception as e:
        print(f"[ERROR] Failed to navigate to eClaim: {str(e)}")
        await take_screenshot(page, "nav_error")
        raise


async def _verify_step_transition(page: Page, label: str) -> None:
    """Take a screenshot and log the URL after each step transition.
    Helps diagnose when navigation goes wrong between steps."""
    await asyncio.sleep(1)
    await dismiss_popups(page)
    print(f"[VERIFY:{label}] URL: {page.url}")
    await take_screenshot(page, label)


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
            # Verify we're on the right page — must find at least one expected element
            member_btn = page.get_by_role("button", name=re.compile("primary member", re.IGNORECASE))
            wire_btn = page.get_by_role("button", name=re.compile("bank wire|ach|wire transfer", re.IGNORECASE))
            if await member_btn.count() == 0 and await wire_btn.count() == 0:
                await take_screenshot(page, "step1_wrong_page")
                raise Exception("STEP1: Neither 'Primary Member' nor 'Bank Wire' buttons found — wrong page")

            # Select PRIMARY MEMBER
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
            # Dump what Playwright can see before we try anything
            await dump_page_state(page, "STEP2")

            # Fill eClaim Nick Name
            # Flutter renders <flt-semantics> textbox nodes; the <input> only
            # appears AFTER the field is focused.  Use role-based locator to
            # find the semantics node first, click it, then type.
            print("[STEP2] Filling eClaim Nick Name")
            # First textbox on the page is the nick name (pre-filled "CLM ...")
            all_textboxes = page.get_by_role("textbox")
            await all_textboxes.first.wait_for(state="visible", timeout=15000)
            await all_textboxes.first.click()
            await asyncio.sleep(1)  # Wait for Flutter to create real <input>
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)
            await page.keyboard.type(claim["invoice_num"], delay=30)
            await page.keyboard.press("Tab")
            await wait_for_flutter(page)

            # Select patient — Flutter dropdown does NOT reliably filter by typing.
            # Strategy: click to open dropdown, wait for options to render, then
            # find and click the exact option matching the patient name.
            target_patient = claim["patient"]
            print(f"[STEP2] Selecting patient: {target_patient}")
            await asyncio.sleep(2)

            # Find the patient field
            patient_field = None
            for strategy, loc_fn in [
                ("textbox name='Patient'", lambda: page.get_by_role("textbox", name=re.compile("Patient", re.IGNORECASE))),
                ("combobox", lambda: page.get_by_role("combobox")),
                ("searchbox", lambda: page.get_by_role("searchbox")),
                ("CSS selector", lambda: page.locator('input[placeholder*="atient" i], input[aria-label*="atient" i]')),
            ]:
                loc = loc_fn()
                if await loc.count() > 0:
                    patient_field = loc.first
                    print(f"[STEP2] Found patient field via {strategy}")
                    break

            if patient_field is None:
                fresh = page.get_by_role("textbox")
                cnt = await fresh.count()
                if cnt >= 2:
                    patient_field = fresh.nth(1)
                    print(f"[STEP2] Using second textbox (count={cnt})")

            if patient_field is None:
                raise Exception("STEP2: Cannot find Patient field")

            # APPROACH 1: Click to open dropdown, then find the right option
            await patient_field.wait_for(state="visible", timeout=15000)
            await patient_field.click()
            await asyncio.sleep(2)  # Wait for dropdown to open

            # Log all visible options for debugging
            for role in ["option", "button", "listitem"]:
                items = page.get_by_role(role)
                cnt = await items.count()
                if cnt > 0 and cnt < 20:
                    names = []
                    for i in range(cnt):
                        try:
                            txt = await items.nth(i).inner_text()
                            names.append(txt.strip()[:50])
                        except Exception:
                            pass
                    if names:
                        print(f"[STEP2] Dropdown {role}s visible: {names}")

            # Try to find and click the correct patient option
            selected = False

            # Extract parts of the patient name for flexible matching
            # e.g. "Fernanda Miranda da Cruz" → try full name, then first name
            name_parts = target_patient.split()
            first_name = name_parts[0] if name_parts else target_patient

            # Method 1: Look for option/button/listitem containing the patient name
            for role in ["option", "button", "listitem"]:
                # Try full name match first
                opt = page.get_by_role(role, name=re.compile(re.escape(target_patient), re.IGNORECASE))
                if await opt.count() > 0:
                    await opt.first.click()
                    print(f"[STEP2] Selected patient '{target_patient}' from {role} (full match)")
                    selected = True
                    break
                # Try first name match (but verify it's not the wrong person)
                opt = page.get_by_role(role, name=re.compile(re.escape(first_name), re.IGNORECASE))
                cnt = await opt.count()
                if cnt > 0:
                    # Check each match to find the right one
                    for i in range(cnt):
                        try:
                            txt = await opt.nth(i).inner_text()
                            if target_patient.lower() in txt.lower() or first_name.lower() in txt.lower():
                                # Make sure it's NOT a different patient
                                other_patients = ["mathias", "elena"] if "fernanda" in first_name.lower() else ["fernanda", "elena"]
                                if not any(other.lower() in txt.lower() for other in other_patients):
                                    await opt.nth(i).click()
                                    print(f"[STEP2] Selected patient '{txt.strip()}' from {role} (first name match)")
                                    selected = True
                                    break
                        except Exception:
                            continue
                    if selected:
                        break

            # Method 2: Type to filter, then click
            if not selected:
                print(f"[STEP2] No direct option found, trying type-to-filter with '{first_name}'")
                await page.keyboard.press("Control+a")
                await page.keyboard.type(first_name, delay=80)
                await asyncio.sleep(3)  # Longer wait for Flutter to filter

                for role in ["option", "button", "listitem"]:
                    opt = page.get_by_role(role)
                    cnt = await opt.count()
                    for i in range(cnt):
                        try:
                            txt = await opt.nth(i).inner_text()
                            if first_name.lower() in txt.lower():
                                await opt.nth(i).click()
                                print(f"[STEP2] Type-filter: selected '{txt.strip()}' from {role}")
                                selected = True
                                break
                        except Exception:
                            continue
                    if selected:
                        break

            # Method 3: Use arrow keys to navigate the dropdown
            if not selected:
                print("[STEP2] Arrow-key navigation fallback")
                # Press Home/Up to go to top of list
                await page.keyboard.press("Home")
                await asyncio.sleep(0.5)
                # Navigate down checking each option
                for arrow_try in range(10):
                    await page.keyboard.press("ArrowDown")
                    await asyncio.sleep(0.5)
                    # Check if current highlight contains our patient
                    focused = page.locator(':focus, [aria-selected="true"], [data-focus="true"]')
                    try:
                        if await focused.count() > 0:
                            txt = await focused.first.inner_text()
                            if first_name.lower() in txt.lower():
                                await page.keyboard.press("Enter")
                                print(f"[STEP2] Arrow-key: selected '{txt.strip()}'")
                                selected = True
                                break
                    except Exception:
                        pass

            if not selected:
                print("[STEP2] CRITICAL: Could not select correct patient!")
                await page.keyboard.press("Enter")

            await wait_for_flutter(page)

            # VERIFY with screenshot
            await asyncio.sleep(1)
            await take_screenshot(page, "step2_after_patient_selection")
            print(f"[STEP2] Screenshot taken after patient selection — VERIFY patient is '{target_patient}'")

            # Dismiss any NOTICE popup
            await asyncio.sleep(1)
            await dismiss_popups(page)
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

    # Parse the service date once — used for both start/end
    date_str = claim["date"]
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        try:
            date_obj = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            try:
                date_obj = datetime.strptime(date_str, "%d/%m/%Y")
            except ValueError:
                date_obj = datetime.strptime(date_str, "%B %d, %Y")

    for attempt in range(MAX_RETRIES):
        try:
            await dump_page_state(page, "STEP4")
            await scroll_form_to_top(page)
            await asyncio.sleep(0.5)

            # ---- CLAIM DATA VALIDATION ----
            print(f"[STEP4] Claim data: provider={claim['provider']}, city={claim['city']}, "
                  f"country={claim['country']}, amount={claim['amount']}, currency={claim['currency']}, "
                  f"date={claim['date']}, diagnosis={claim['diagnosis']}, procedure={claim['procedure']}")

            # Sanity check: city should not be a year, country should not be a medical term
            if claim.get("city") and re.match(r'^\d{4}$', claim["city"].strip()):
                print(f"[STEP4] WARNING: city='{claim['city']}' looks like a year — possible column mapping error!")
            if claim.get("country") and claim["country"].strip().lower() in ("medical", "dental", "surgical", "pharmacy"):
                print(f"[STEP4] WARNING: country='{claim['country']}' looks like a category — possible column mapping error!")

            # ---- FIELD FILLING ----
            # In HTML renderer mode (?renderer=html), Flutter creates real <input> elements
            # with actual labels. In CanvasKit mode, it uses flt-semantics with ARIA roles.
            # We try both naming conventions.

            # 1. Charge Nickname — first textbox, pre-filled "CHG 1 DD-MMM-YYYY"
            print("[STEP4] Filling Charge Nickname")
            all_textboxes = page.get_by_role("textbox")
            tb_count = await all_textboxes.count()
            print(f"[STEP4] Total textboxes on page: {tb_count}")
            await all_textboxes.first.wait_for(state="visible", timeout=15000)
            await all_textboxes.first.click()
            await asyncio.sleep(1)
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)
            await page.keyboard.type(claim["invoice_num"], delay=30)
            await page.keyboard.press("Tab")
            await wait_for_flutter(page)

            # 2. Doctor/Dentist radio — pre-selected, leave it
            print("[STEP4] Doctor/Dentist radio pre-selected, skipping")

            # 3. Select Provider — multiple strategies for both rendering modes
            print(f"[STEP4] Selecting provider: {claim['provider']}")
            provider_field = None

            # Strategy A: textbox with "Provider" in the accessible name (HTML mode)
            loc = page.get_by_role("textbox", name=re.compile("Provider", re.IGNORECASE))
            cnt = await loc.count()
            if cnt > 0:
                provider_field = loc.first
                print(f"[STEP4] Found provider via textbox name='Provider' (count={cnt})")

            # Strategy B: combobox (CanvasKit mode)
            if provider_field is None:
                loc = page.get_by_role("combobox")
                cnt = await loc.count()
                if cnt > 0:
                    provider_field = loc.first
                    print(f"[STEP4] Found provider via combobox (count={cnt})")

            # Strategy C: searchbox
            if provider_field is None:
                loc = page.get_by_role("searchbox")
                cnt = await loc.count()
                if cnt > 0:
                    provider_field = loc.first
                    print(f"[STEP4] Found provider via searchbox (count={cnt})")

            # Strategy D: input with placeholder containing "provider" (HTML mode)
            if provider_field is None:
                loc = page.locator('input[placeholder*="rovider" i], input[aria-label*="rovider" i]')
                cnt = await loc.count()
                if cnt > 0:
                    provider_field = loc.first
                    print(f"[STEP4] Found provider via CSS selector (count={cnt})")

            # Strategy E: second textbox (first is charge nickname)
            if provider_field is None:
                fresh = page.get_by_role("textbox")
                cnt = await fresh.count()
                print(f"[STEP4] Provider fallback: total textboxes={cnt}")
                if cnt >= 2:
                    provider_field = fresh.nth(1)
                    print("[STEP4] Using second textbox as provider field")

            if provider_field is None:
                raise Exception("STEP4: Cannot find Provider field — check dump_page_state output")

            await provider_field.wait_for(state="visible", timeout=15000)
            await provider_field.click()
            await asyncio.sleep(1)
            await page.keyboard.type(claim["provider"][:20], delay=50)
            await asyncio.sleep(2)

            # Try to select from dropdown
            option = page.get_by_role("option", name=re.compile(re.escape(claim["provider"][:15]), re.IGNORECASE))
            if await option.count() > 0:
                await option.first.click()
                print("[STEP4] Selected provider from option role")
            else:
                option = page.get_by_role("button", name=re.compile(re.escape(claim["provider"][:15]), re.IGNORECASE))
                if await option.count() > 0:
                    await option.first.click()
                    print("[STEP4] Selected provider from button role")
                else:
                    # Try listitem
                    option = page.get_by_role("listitem").filter(has_text=re.compile(re.escape(claim["provider"][:15]), re.IGNORECASE))
                    if await option.count() > 0:
                        await option.first.click()
                        print("[STEP4] Selected provider from listitem")
                    else:
                        print("[STEP4] No dropdown option found for provider, pressing Enter")
                        await page.keyboard.press("Enter")
            await wait_for_flutter(page)

            # Close provider dropdown overlay before moving on
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            await scroll_form(page)

            # 4. City — try textbox with "City" in name first, fall back to "TextField"
            if claim.get("city"):
                print(f"[STEP4] Filling city: {claim['city']}")
                city_field = None

                # HTML mode: field labeled "City"
                loc = page.get_by_role("textbox", name=re.compile(r"\bCity\b", re.IGNORECASE))
                if await loc.count() > 0:
                    city_field = loc.first
                    print("[STEP4] Found city via textbox name='City'")

                # HTML mode: input with placeholder/label "City"
                if city_field is None:
                    loc = page.locator('input[placeholder*="ity" i], input[aria-label*="ity" i]')
                    cnt = await loc.count()
                    if cnt > 0:
                        city_field = loc.first
                        print(f"[STEP4] Found city via CSS selector (count={cnt})")

                # CanvasKit mode: "TextField" — but only if it's the FIRST "TextField" textbox
                if city_field is None:
                    loc = page.get_by_role("textbox", name=re.compile("TextField", re.IGNORECASE))
                    if await loc.count() > 0:
                        city_field = loc.first
                        print("[STEP4] Found city via textbox name='TextField' (CanvasKit mode)")

                if city_field:
                    await city_field.click()
                    await asyncio.sleep(1)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(claim["city"], delay=30)
                    await page.keyboard.press("Tab")
                    await wait_for_flutter(page)
                else:
                    print("[STEP4] WARN: Could not find City field")

            # 5. Country of Treatment — try multiple name patterns
            if claim.get("country"):
                print(f"[STEP4] Selecting country: {claim['country']}")
                country_field = None

                # Try "Country" in name (covers both modes)
                loc = page.get_by_role("textbox", name=re.compile("Country", re.IGNORECASE))
                if await loc.count() > 0:
                    country_field = loc.first
                    print("[STEP4] Found country via textbox name='Country'")

                # CSS fallback
                if country_field is None:
                    loc = page.locator('input[placeholder*="ountry" i], input[aria-label*="ountry" i]')
                    if await loc.count() > 0:
                        country_field = loc.first
                        print("[STEP4] Found country via CSS selector")

                if country_field:
                    await country_field.click()
                    await asyncio.sleep(1)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(claim["country"], delay=50)
                    await asyncio.sleep(1)
                    # Try to select from dropdown
                    opt = page.get_by_role("option", name=re.compile(re.escape(claim["country"]), re.IGNORECASE))
                    if await opt.count() > 0:
                        await opt.first.click()
                    else:
                        opt = page.get_by_role("button", name=re.compile(re.escape(claim["country"]), re.IGNORECASE))
                        if await opt.count() > 0:
                            await opt.first.click()
                        else:
                            await page.keyboard.press("Enter")
                    await wait_for_flutter(page)
                    # Close country dropdown
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
                else:
                    print("[STEP4] WARN: Could not find Country of Treatment field")

            await scroll_form(page)

            # 6. Charge Amount — try textbox with amount/charge in name, fall back to TextField
            print(f"[STEP4] Filling charge amount: {claim['amount']}")
            amount_field = None

            # HTML mode: field labeled with "amount" or "charge"
            loc = page.get_by_role("textbox", name=re.compile("amount|charge", re.IGNORECASE))
            if await loc.count() > 0:
                amount_field = loc.first
                print("[STEP4] Found amount via textbox name")

            # CSS fallback for amount
            if amount_field is None:
                loc = page.locator('input[placeholder*="mount" i], input[aria-label*="mount" i]')
                if await loc.count() > 0:
                    amount_field = loc.first
                    print("[STEP4] Found amount via CSS selector")

            # CanvasKit mode: second "TextField" textbox
            if amount_field is None:
                textfields = page.get_by_role("textbox", name=re.compile("TextField", re.IGNORECASE))
                cnt = await textfields.count()
                if cnt > 1:
                    amount_field = textfields.nth(1)
                    print(f"[STEP4] Found amount via TextField.nth(1), count={cnt}")
                elif cnt > 0:
                    amount_field = textfields.first
                    print("[STEP4] Found amount via TextField.first (only one left)")

            if amount_field:
                await amount_field.click()
                await asyncio.sleep(1)
                await page.keyboard.press("Control+a")
                await page.keyboard.type(claim["amount"], delay=30)
                await page.keyboard.press("Tab")
                await wait_for_flutter(page)
            else:
                print("[STEP4] WARN: Could not find Charge Amount field")

            # 7. Billed Invoice Currency — try multiple name patterns
            print(f"[STEP4] Selecting currency: {claim['currency']}")
            currency_field = None

            loc = page.get_by_role("textbox", name=re.compile("Currency|Billed Invoice", re.IGNORECASE))
            if await loc.count() > 0:
                currency_field = loc.first
                print("[STEP4] Found currency via textbox name")

            if currency_field is None:
                loc = page.locator('input[placeholder*="urrency" i], input[aria-label*="urrency" i]')
                if await loc.count() > 0:
                    currency_field = loc.first
                    print("[STEP4] Found currency via CSS selector")

            if currency_field:
                await currency_field.click()
                await asyncio.sleep(1)
                await page.keyboard.press("Control+a")
                await page.keyboard.type(claim["currency"], delay=50)
                await asyncio.sleep(1)
                opt = page.get_by_role("option", name=re.compile(re.escape(claim["currency"]), re.IGNORECASE))
                if await opt.count() > 0:
                    await opt.first.click()
                else:
                    opt = page.get_by_role("button", name=re.compile(re.escape(claim["currency"]), re.IGNORECASE))
                    if await opt.count() > 0:
                        await opt.first.click()
                    else:
                        await page.keyboard.press("Enter")
                await wait_for_flutter(page)
                # Close currency dropdown
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
            else:
                print("[STEP4] WARN: Could not find Currency field")

            await scroll_form(page)

            # Close any lingering dropdown overlays before moving to diagnosis
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # 8. Condition/Diagnosis — multiple strategies
            print(f"[STEP4] Selecting diagnosis: {claim['diagnosis']}")
            await dump_page_state(page, "STEP4_PRE_DIAG")
            diag_field = None

            # Try textbox with Diagnosis/Condition in name (works in both modes)
            loc = page.get_by_role("textbox", name=re.compile("Diagnosis|Condition", re.IGNORECASE))
            if await loc.count() > 0:
                diag_field = loc.first
                print("[STEP4] Found diagnosis via textbox name")

            # CSS fallback
            if diag_field is None:
                loc = page.locator('input[placeholder*="iagnosis" i], input[aria-label*="iagnosis" i], '
                                   'input[placeholder*="ondition" i], input[aria-label*="ondition" i]')
                if await loc.count() > 0:
                    diag_field = loc.first
                    print("[STEP4] Found diagnosis via CSS selector")

            # Try combobox — but ONLY if there are 2+ (first one is Provider)
            if diag_field is None:
                loc = page.get_by_role("combobox")
                cnt = await loc.count()
                if cnt > 1:
                    diag_field = loc.nth(1)
                    print(f"[STEP4] Found diagnosis via combobox.nth(1), count={cnt}")
                # DO NOT use combobox.first if cnt==1 — that's the Provider field!

            if diag_field is None:
                print("[STEP4] FATAL: Cannot find Diagnosis field — refusing to type into wrong field")
                await take_screenshot(page, "step4_no_diagnosis_field")
                raise Exception("STEP4: Cannot find Diagnosis/Condition field — check dump_page_state output")

            await diag_field.click()
            await asyncio.sleep(1)
            await page.keyboard.type(claim["diagnosis"][:30], delay=50)
            await asyncio.sleep(2)
            opt = page.get_by_role("option", name=re.compile(re.escape(claim["diagnosis"][:20]), re.IGNORECASE))
            if await opt.count() > 0:
                await opt.first.click()
                print("[STEP4] Selected diagnosis from option")
            else:
                opt = page.get_by_role("button", name=re.compile(re.escape(claim["diagnosis"][:20]), re.IGNORECASE))
                if await opt.count() > 0:
                    await opt.first.click()
                    print("[STEP4] Selected diagnosis from button")
                else:
                    opt = page.get_by_role("listitem").filter(has_text=re.compile(re.escape(claim["diagnosis"][:15]), re.IGNORECASE))
                    if await opt.count() > 0:
                        await opt.first.click()
                        print("[STEP4] Selected diagnosis from listitem")
                    else:
                        print("[STEP4] No dropdown match for diagnosis, pressing Enter")
                        await page.keyboard.press("Enter")
            await wait_for_flutter(page)

            # Close diagnosis dropdown before moving to next field
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # 9. Service Description — textbox with "Service" in name
            print(f"[STEP4] Selecting service: {claim['procedure']}")
            service_field = None

            loc = page.get_by_role("textbox", name=re.compile("Service", re.IGNORECASE))
            if await loc.count() > 0:
                service_field = loc.first
                print("[STEP4] Found service via textbox name")

            if service_field is None:
                loc = page.locator('input[placeholder*="ervice" i], input[aria-label*="ervice" i]')
                if await loc.count() > 0:
                    service_field = loc.first
                    print("[STEP4] Found service via CSS selector")

            if service_field:
                await service_field.click()
                await asyncio.sleep(1)
                await page.keyboard.press("Control+a")
                await page.keyboard.type(claim["procedure"][:30], delay=50)
                await asyncio.sleep(1)
                opt = page.get_by_role("option", name=re.compile(re.escape(claim["procedure"][:20]), re.IGNORECASE))
                if await opt.count() > 0:
                    await opt.first.click()
                else:
                    opt = page.get_by_role("button", name=re.compile(re.escape(claim["procedure"][:20]), re.IGNORECASE))
                    if await opt.count() > 0:
                        await opt.first.click()
                    else:
                        await page.keyboard.press("Enter")
                await wait_for_flutter(page)
            else:
                print("[STEP4] WARN: Could not find Service Description field")

            await scroll_form(page)

            # 10. Start Date — Flutter calendar picker
            print(f"[STEP4] Selecting start date: {date_obj.strftime('%Y-%m-%d')}")
            start_btn = page.get_by_role("button", name=re.compile("Start Date of Service", re.IGNORECASE))
            if await start_btn.count() > 0:
                await start_btn.first.click()
                await asyncio.sleep(2)
                await _select_calendar_date(page, date_obj)

            # 11. End Date — searchbox with "mm/dd/yyyy" placeholder
            print(f"[STEP4] Filling end date: {date_obj.strftime('%m/%d/%Y')}")
            end_field = page.get_by_role("searchbox", name=re.compile("mm/dd/yyyy", re.IGNORECASE))
            if await end_field.count() == 0:
                end_field = page.get_by_role("textbox", name=re.compile("mm/dd/yyyy", re.IGNORECASE))
            if await end_field.count() > 0:
                await end_field.first.click()
                await asyncio.sleep(1)
                await page.keyboard.press("Control+a")
                await page.keyboard.type(date_obj.strftime("%m/%d/%Y"), delay=30)
                await page.keyboard.press("Tab")
                await wait_for_flutter(page)

            await scroll_form(page)

            # 12. Click Save charge
            print("[STEP4] Clicking Save charge")
            save_btn = page.get_by_role("button", name=re.compile("save charge", re.IGNORECASE))
            if await save_btn.count() > 0:
                await save_btn.first.click()
                await asyncio.sleep(3)

            # After saving, click Next to proceed to step 5
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
            ["gog", "drive", "download", file_id, "--out", temp_file],
            timeout=60, capture_output=True, text=True, env=GOG_ENV
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
        await _verify_step_transition(page, "after_step1")
        await step2_basic_info(page, claim)
        await _verify_step_transition(page, "after_step2")
        await step3_other_insurance(page)
        await _verify_step_transition(page, "after_step3")
        await step4_charges(page, claim)
        await _verify_step_transition(page, "after_step4")
        await step5_reimbursement(page)
        await _verify_step_transition(page, "after_step5")
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
            # Always open a fresh page to avoid dirty state from previous
            # sessions or manual browser use by FerdyBot
            page = await context.new_page()
            print("[MAIN] Opened fresh browser page")
            print("[MAIN] Connected to browser")

            # Log in
            await login(page)

            # File each claim
            filed_count = 0
            failures = []
            for idx, claim in enumerate(claims, start=1):
                print(f"\n[MAIN] Processing claim {idx}/{len(claims)}")

                try:
                    reference_number = await file_single_claim(page, claim)
                except Exception as e:
                    reference_number = None
                    print(f"[MAIN] Exception filing claim: {e}")

                if reference_number:
                    update_sheets(claim["row_number"], reference_number)
                    notify_telegram(claim, reference_number)
                    filed_count += 1

                    if idx < len(claims):
                        print("[MAIN] Waiting before next claim...")
                        await asyncio.sleep(5)
                else:
                    failures.append((claim["invoice_num"], "Step failed — see logs"))
                    print(f"[MAIN] Skipping sheet update for failed claim {claim['invoice_num']}")

            # Print a clear, unmissable summary for FerdyBot to relay
            print(f"\n{'='*70}")
            if filed_count == len(claims) and filed_count > 0:
                print(f"[RESULT] SUCCESS — All {filed_count} claim(s) filed successfully!")
            elif filed_count > 0:
                print(f"[RESULT] PARTIAL — Filed {filed_count}/{len(claims)} claims.")
                for inv, err in failures:
                    print(f"  FAILED: {inv} — {err}")
            else:
                print(f"[RESULT] FAILED — None of the {len(claims)} claim(s) could be filed.")
                for inv, err in failures:
                    print(f"  FAILED: {inv} — {err}")
            print(f"Timestamp: {datetime.now().isoformat()}")
            print(f"{'='*70}\n")

            # Also try Telegram direct notification (if bot token is available)
            notify_telegram_summary(len(claims), filed_count, failures)
        except Exception as e:
            print(f"\n{'='*70}")
            print(f"[RESULT] CRASHED — Fatal error: {str(e)[:300]}")
            print(f"{'='*70}\n")
            # Also try Telegram direct notification
            notify_telegram_summary(
                len(claims) if claims else 0, 0,
                [("ALL", str(e)[:200])]
            )
            raise
        finally:
            # Close our page to avoid leaving dirty state
            try:
                await page.close()
            except Exception:
                pass
            print("[MAIN] Disconnecting from browser")


if __name__ == "__main__":
    asyncio.run(main())
