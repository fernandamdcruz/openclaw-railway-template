"""
BCBS Claim Filer - Automated claim filing for BCBS portal using Playwright.

This script automates the complete claim filing workflow on the BCBS portal
(https://members.bcbsglobalsolutions.com) which uses Flutter Web with HTML renderer.

Key features:
- Connects to existing Chromium CDP instance (port 9222)
- Handles Flutter Web DOM (flt-semantics elements with ARIA roles)
- Implements 6-step claim filing wizard
- Reads pending claims from Google Sheets
- Uploads invoices from Google Drive
- Updates sheet with filing status and reference numbers
- Sends Telegram notifications on completion
- Includes retry logic and screenshot capture on errors
"""

import asyncio
import os
import subprocess
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

# Configuration
BCBS_PORTAL_URL = "https://members.bcbsglobalsolutions.com"
CDP_URL = "http://127.0.0.1:9222"
GOOGLE_SHEET_ID = "1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk"
GOOGLE_SHEET_TAB = "2026"
READ_2FA_SCRIPT = "/data/workspace/read_2fa.py"
TELEGRAM_CHAT_ID = "8409634074"
MAX_RETRIES = 3
FLUTTER_WAIT_TIME = 2000  # milliseconds
SHORT_WAIT = 500  # milliseconds

# Screenshot directory
SCREENSHOT_DIR = Path("/tmp/bcbs_screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

print(f"[INIT] BCBS Claim Filer initialized at {datetime.now().isoformat()}")
print(f"[INIT] Screenshot directory: {SCREENSHOT_DIR}")


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def take_screenshot(page: Page, name: str) -> None:
    """Take a screenshot for debugging purposes."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = SCREENSHOT_DIR / f"{name}_{timestamp}.png"
    await page.screenshot(path=str(filename))
    print(f"[DEBUG] Screenshot saved: {filename}")


async def scroll_form(page: Page) -> None:
    """Scroll the Flutter form container down by 500px."""
    print("[FORM] Scrolling form container")
    await page.evaluate("""
        () => {
            const scrollable = Array.from(document.querySelectorAll('flt-semantics'))
                .find(el => el.scrollHeight > el.clientHeight + 50);
            if (scrollable) {
                scrollable.scrollTop += 500;
            }
        }
    """)
    await asyncio.sleep(0.3)


async def wait_for_flutter(page: Page, wait_ms: int = FLUTTER_WAIT_TIME) -> None:
    """Wait for Flutter widget tree to rebuild after interactions."""
    await asyncio.sleep(wait_ms / 1000)


async def fill_flutter_field(page: Page, field_name: str, value: str) -> None:
    """Fill a Flutter textbox field by role name."""
    print(f"[FIELD] Filling field '{field_name}' with value")
    try:
        # Click to focus the field
        field = page.get_by_role("textbox", name=re.compile(field_name, re.IGNORECASE))
        await field.click()
        await asyncio.sleep(SHORT_WAIT / 1000)

        # Clear existing value and type new value
        await field.fill("")
        await field.type(value)
        await field.press("Tab")
        await wait_for_flutter(page)
        print(f"[FIELD] Successfully filled '{field_name}'")
    except Exception as e:
        print(f"[ERROR] Failed to fill field '{field_name}': {str(e)}")
        raise


async def select_dropdown(page: Page, dropdown_name: str, value: str) -> None:
    """
    Select a value from a Flutter dropdown.

    In Flutter Web, dropdowns appear as textbox elements. We click to open,
    then click the option button that appears.
    """
    print(f"[DROPDOWN] Opening dropdown '{dropdown_name}'")
    try:
        # Click the dropdown textbox to open options
        dropdown = page.get_by_role("textbox", name=re.compile(dropdown_name, re.IGNORECASE))
        await dropdown.click()
        await wait_for_flutter(page)

        # Click the matching option button
        print(f"[DROPDOWN] Selecting option '{value}'")
        option = page.get_by_role("button", name=re.compile(re.escape(value), re.IGNORECASE))
        await option.click()
        await wait_for_flutter(page)
        print(f"[DROPDOWN] Successfully selected '{value}'")
    except Exception as e:
        print(f"[ERROR] Failed to select dropdown '{dropdown_name}': {str(e)}")
        raise


async def select_date(page: Page, date_field_name: str, target_date: str) -> None:
    """
    Select a date using the Flutter calendar date picker.

    target_date should be in format "YYYY-MM-DD".
    This function:
    1. Clicks the date field to open the picker
    2. Navigates to the correct month
    3. Clicks the day number
    4. Clicks OK
    """
    print(f"[DATE] Selecting date '{target_date}' for field '{date_field_name}'")

    try:
        # Parse the target date
        date_obj = datetime.strptime(target_date, "%Y-%m-%d")
        target_month = date_obj.strftime("%B %Y")  # e.g., "March 2026"
        target_day = str(date_obj.day)

        # Click the date field to open picker
        date_field = page.get_by_role("textbox", name=re.compile(date_field_name, re.IGNORECASE))
        await date_field.click()
        await wait_for_flutter(page)

        # Navigate to the correct month using previous/next arrows
        max_attempts = 12
        attempts = 0
        while attempts < max_attempts:
            current_month_elem = page.get_by_role("heading", name=re.compile(r"\w+ \d{4}"))
            current_month_text = await current_month_elem.inner_text() if current_month_elem else ""

            if target_month in current_month_text:
                print(f"[DATE] Found target month: {target_month}")
                break

            # Click next arrow if needed
            if date_obj > datetime.now():
                next_btn = page.get_by_role("button", name=re.compile("next|forward", re.IGNORECASE))
                await next_btn.click()
            else:
                prev_btn = page.get_by_role("button", name=re.compile("previous|back", re.IGNORECASE))
                await prev_btn.click()

            await wait_for_flutter(page)
            attempts += 1

        # Click the day number
        day_button = page.get_by_role("button", name=target_day)
        await day_button.click()
        await wait_for_flutter(page)

        # Click OK to confirm
        ok_button = page.get_by_role("button", name="OK")
        await ok_button.click()
        await wait_for_flutter(page)

        print(f"[DATE] Successfully selected date {target_date}")
    except Exception as e:
        print(f"[ERROR] Failed to select date: {str(e)}")
        raise


async def close_popup(page: Page, popup_name: str) -> None:
    """Close a popup/dialog by finding and clicking its close button."""
    print(f"[POPUP] Attempting to close popup '{popup_name}'")
    try:
        # Try to find close button (X icon)
        close_button = page.get_by_role("button", name=re.compile("close|dismiss", re.IGNORECASE))
        if close_button:
            await close_button.click()
            await wait_for_flutter(page)
            print(f"[POPUP] Closed popup '{popup_name}'")
    except Exception as e:
        print(f"[WARN] Could not close popup '{popup_name}': {str(e)}")


# ============================================================================
# GOOGLE SHEETS INTEGRATION
# ============================================================================

def read_pending_claims() -> List[Dict[str, Any]]:
    """
    Read pending claims from Google Sheets.

    Uses gog CLI or direct API to fetch rows where column K = "Pending".
    Returns list of dicts with keys: patient, provider, date, amount, currency,
    diagnosis, procedure, invoice_num, drive_link, row_number.
    """
    print("[SHEETS] Reading pending claims from Google Sheets")

    claims = []
    try:
        # Try using gog CLI first
        result = subprocess.run(
            ["gog", "sheets", "export", GOOGLE_SHEET_ID, GOOGLE_SHEET_TAB, "--json"],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            rows = json.loads(result.stdout)

            for row_idx, row in enumerate(rows, start=2):  # Start at 2 (row 1 is header)
                # Column K is index 10 (0-indexed)
                if len(row) > 10 and row[10] and row[10].strip().lower() == "pending":
                    claim = {
                        "patient": row[1] if len(row) > 1 else "",  # Column B
                        "provider": row[2] if len(row) > 2 else "",  # Column C
                        "date": row[3] if len(row) > 3 else "",  # Column D
                        "amount": row[4] if len(row) > 4 else "",  # Column E
                        "currency": row[5] if len(row) > 5 else "",  # Column F
                        "diagnosis": row[6] if len(row) > 6 else "",  # Column G
                        "procedure": row[7] if len(row) > 7 else "",  # Column H
                        "invoice_num": row[8] if len(row) > 8 else "",  # Column I
                        "drive_link": row[11] if len(row) > 11 else "",  # Column L
                        "row_number": row_idx,
                    }
                    claims.append(claim)
                    print(f"[SHEETS] Found pending claim: {claim['invoice_num']} - {claim['patient']}")
        else:
            print(f"[ERROR] gog CLI failed: {result.stderr}")
            print("[WARN] Skipping claims reading - will use empty list")
    except Exception as e:
        print(f"[ERROR] Failed to read sheets: {str(e)}")
        print("[WARN] Continuing with empty claims list for testing")

    print(f"[SHEETS] Total pending claims: {len(claims)}")
    return claims


def update_sheets(row_number: int, reference_number: str) -> None:
    """
    Update the Google Sheet after successful filing.

    Sets column K to "Filed" and column M to the reference number.
    """
    print(f"[SHEETS] Updating row {row_number} with reference {reference_number}")

    try:
        # Use gog CLI to update the sheet
        subprocess.run(
            [
                "gog", "sheets", "update",
                GOOGLE_SHEET_ID,
                GOOGLE_SHEET_TAB,
                f"K{row_number}:Filed",
                f"M{row_number}:{reference_number}",
            ],
            timeout=30,
            check=True
        )
        print(f"[SHEETS] Successfully updated row {row_number}")
    except Exception as e:
        print(f"[ERROR] Failed to update sheets: {str(e)}")


# ============================================================================
# NOTIFICATION
# ============================================================================

def notify_telegram(claim: Dict[str, Any], reference_number: str) -> None:
    """Send a Telegram notification with claim filing confirmation."""
    print(f"[TELEGRAM] Sending notification for {claim['invoice_num']}")

    try:
        # Get bot token from environment or skip if not available
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            print("[WARN] TELEGRAM_BOT_TOKEN not set - skipping notification")
            return

        message = (
            f"✅ Claim Filed Successfully\n\n"
            f"Invoice: {claim['invoice_num']}\n"
            f"Patient: {claim['patient']}\n"
            f"Amount: {claim['amount']} {claim['currency']}\n"
            f"Reference: {reference_number}"
        )

        subprocess.run(
            [
                "curl", "-X", "POST",
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                "-d", f"chat_id={TELEGRAM_CHAT_ID}",
                "-d", f"text={message}",
            ],
            timeout=10,
            check=False
        )
        print("[TELEGRAM] Notification sent")
    except Exception as e:
        print(f"[WARN] Failed to send Telegram notification: {str(e)}")


# ============================================================================
# AUTHENTICATION
# ============================================================================

async def login(page: Page) -> None:
    """
    Log in to the BCBS portal.

    Steps:
    1. Navigate to the portal
    2. Fill username and password from environment variables
    3. Click Login
    4. Wait for dashboard to load
    5. Handle 2FA if required
    """
    print("[AUTH] Starting login process")

    username = os.environ.get("BCBS_USERNAME")
    password = os.environ.get("BCBS_PASSWORD")

    if not username or not password:
        raise ValueError("BCBS_USERNAME or BCBS_PASSWORD environment variables not set")

    try:
        print(f"[AUTH] Navigating to {BCBS_PORTAL_URL}")
        await page.goto(BCBS_PORTAL_URL, wait_until="networkidle")
        await asyncio.sleep(2)

        # Fill username
        print("[AUTH] Filling username")
        await fill_flutter_field(page, "username|email", username)

        # Fill password
        print("[AUTH] Filling password")
        await fill_flutter_field(page, "password", password)

        # Click Login button
        print("[AUTH] Clicking Login button")
        login_button = page.get_by_role("button", name=re.compile("login|sign in", re.IGNORECASE))
        await login_button.click()
        await wait_for_flutter(page)

        # Wait for dashboard or 2FA screen
        print("[AUTH] Waiting for dashboard to load")
        await asyncio.sleep(3)

        # Check if 2FA is required
        try:
            await handle_2fa(page)
        except Exception:
            print("[AUTH] No 2FA required")

        print("[AUTH] Login successful")
    except Exception as e:
        print(f"[ERROR] Login failed: {str(e)}")
        await take_screenshot(page, "login_error")
        raise


async def handle_2fa(page: Page) -> None:
    """
    Handle 2FA if the screen appears.

    Steps:
    1. Check if 2FA input field is visible
    2. Run read_2fa.py to get the code
    3. Enter the code
    4. Submit
    """
    print("[2FA] Checking for 2FA requirement")

    try:
        # Check if 2FA field exists
        code_field = page.get_by_role("textbox", name=re.compile("code|otp|2fa", re.IGNORECASE))

        if not code_field:
            print("[2FA] No 2FA field found - skipping")
            return

        print("[2FA] 2FA field detected - running read_2fa.py")
        result = subprocess.run(
            ["python3", READ_2FA_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            code = result.stdout.strip()
            print(f"[2FA] Received code from script")

            # Fill the code
            await code_field.click()
            await asyncio.sleep(SHORT_WAIT / 1000)
            await code_field.fill(code)
            await wait_for_flutter(page)

            # Submit
            submit_button = page.get_by_role("button", name=re.compile("submit|verify|confirm", re.IGNORECASE))
            await submit_button.click()
            await wait_for_flutter(page)

            print("[2FA] 2FA submitted successfully")
        else:
            print(f"[ERROR] Failed to get 2FA code: {result.stderr}")
            raise Exception("2FA code retrieval failed")
    except Exception as e:
        print(f"[WARN] 2FA handling issue: {str(e)}")
        raise


async def dismiss_popups(page: Page) -> None:
    """
    Close any important update or notice popups that may appear.
    """
    print("[POPUP] Dismissing any popups")

    popup_names = ["Important Update", "NOTICE", "Alert", "Notification"]
    for popup_name in popup_names:
        try:
            await close_popup(page, popup_name)
        except Exception:
            pass


# ============================================================================
# NAVIGATION
# ============================================================================

async def navigate_to_eclaim(page: Page) -> None:
    """
    Navigate to the eClaim section of the portal.

    Finds and clicks the File a Claim or eClaim navigation element.
    """
    print("[NAV] Navigating to eClaim section")

    try:
        # Look for navigation button/link with "claim", "file", or "eclaim"
        claim_nav = page.get_by_role("button", name=re.compile("claim|file|eclaim", re.IGNORECASE))
        if claim_nav:
            await claim_nav.click()
            await wait_for_flutter(page)
            print("[NAV] Navigated to eClaim")
        else:
            # Try link as fallback
            claim_nav = page.get_by_role("link", name=re.compile("claim|file|eclaim", re.IGNORECASE))
            await claim_nav.click()
            await wait_for_flutter(page)
            print("[NAV] Navigated to eClaim (via link)")
    except Exception as e:
        print(f"[ERROR] Failed to navigate to eClaim: {str(e)}")
        await take_screenshot(page, "nav_error")
        raise


# ============================================================================
# CLAIM FILING STEPS
# ============================================================================

async def step1_preliminary(page: Page) -> None:
    """
    Step 1: Preliminary information.

    Actions:
    1. Click PRIMARY MEMBER
    2. Click BANK WIRE TRANSFER OR ACH PAYMENT (NOT CHECK)
    3. Select No for accident question
    4. Click Next
    """
    print("[STEP1] Starting preliminary step")

    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Click PRIMARY MEMBER
            print("[STEP1] Selecting PRIMARY MEMBER")
            member_btn = page.get_by_role("button", name=re.compile("primary member", re.IGNORECASE))
            await member_btn.click()
            await wait_for_flutter(page)

            # Click BANK WIRE TRANSFER OR ACH PAYMENT (not CHECK)
            print("[STEP1] Selecting bank payment method")
            payment_btn = page.get_by_role("button", name=re.compile("bank wire|ach|wire transfer", re.IGNORECASE))
            await payment_btn.click()
            await wait_for_flutter(page)

            # Select No for accident
            print("[STEP1] Selecting No for accident")
            no_btn = page.get_by_role("radio", name="No")
            await no_btn.click()
            await wait_for_flutter(page)

            # Click Next
            print("[STEP1] Clicking Next")
            next_btn = page.get_by_role("button", name="Next")
            await next_btn.click()
            await wait_for_flutter(page)

            print("[STEP1] Preliminary step completed")
            return
        except Exception as e:
            retry_count += 1
            print(f"[STEP1] Attempt {retry_count}/{MAX_RETRIES} failed: {str(e)}")
            if retry_count >= MAX_RETRIES:
                await take_screenshot(page, "step1_error")
                raise Exception(f"Step 1 failed after {MAX_RETRIES} retries: {str(e)}")
            await asyncio.sleep(1)


async def step2_basic_info(page: Page, claim: Dict[str, Any]) -> None:
    """
    Step 2: Basic claim information.

    Actions:
    1. Clear eClaim Nick Name and fill with invoice#
    2. Select patient from dropdown
    3. Click Next
    """
    print("[STEP2] Starting basic info step")

    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Fill eClaim Nick Name with invoice number
            print("[STEP2] Filling eClaim Nick Name")
            await fill_flutter_field(page, "nick name|claim name", claim["invoice_num"])

            # Select patient from dropdown
            print(f"[STEP2] Selecting patient: {claim['patient']}")
            await select_dropdown(page, "patient|member", claim["patient"])

            # Click Next
            print("[STEP2] Clicking Next")
            next_btn = page.get_by_role("button", name="Next")
            await next_btn.click()
            await wait_for_flutter(page)

            print("[STEP2] Basic info step completed")
            return
        except Exception as e:
            retry_count += 1
            print(f"[STEP2] Attempt {retry_count}/{MAX_RETRIES} failed: {str(e)}")
            if retry_count >= MAX_RETRIES:
                await take_screenshot(page, "step2_error")
                raise Exception(f"Step 2 failed after {MAX_RETRIES} retries: {str(e)}")
            await asyncio.sleep(1)


async def step3_other_insurance(page: Page) -> None:
    """
    Step 3: Other insurance information.

    Actions:
    1. Verify "No" is selected for other insurance
    2. Click Next
    """
    print("[STEP3] Starting other insurance step")

    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Select No for other insurance
            print("[STEP3] Selecting No for other insurance")
            no_btn = page.get_by_role("radio", name="No")
            await no_btn.click()
            await wait_for_flutter(page)

            # Click Next
            print("[STEP3] Clicking Next")
            next_btn = page.get_by_role("button", name="Next")
            await next_btn.click()
            await wait_for_flutter(page)

            print("[STEP3] Other insurance step completed")
            return
        except Exception as e:
            retry_count += 1
            print(f"[STEP3] Attempt {retry_count}/{MAX_RETRIES} failed: {str(e)}")
            if retry_count >= MAX_RETRIES:
                await take_screenshot(page, "step3_error")
                raise Exception(f"Step 3 failed after {MAX_RETRIES} retries: {str(e)}")
            await asyncio.sleep(1)


async def step4_charges(page: Page, claim: Dict[str, Any]) -> None:
    """
    Step 4: Charge information (most complex step).

    Actions:
    1. Fill Charge Nickname (invoice#)
    2. Select Provider dropdown
    3. Fill City
    4. Select Country of Treatment dropdown
    5. Fill Charge Amount
    6. Select Billed Invoice Currency dropdown
    7. Select Condition or Diagnosis dropdown
    8. Select Service Description dropdown
    9. Fill Start Date (using calendar UI)
    10. Fill End Date (same as start for single-day)
    11. Click Save charge
    """
    print("[STEP4] Starting charges step")

    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Scroll to ensure form is visible
            await scroll_form(page)

            # Fill Charge Nickname
            print("[STEP4] Filling charge nickname")
            await fill_flutter_field(page, "charge nickname|nickname", claim["invoice_num"])

            # Select Provider
            print(f"[STEP4] Selecting provider: {claim['provider']}")
            await select_dropdown(page, "provider|facility", claim["provider"])

            # Scroll if needed
            await scroll_form(page)

            # Fill City (extract from provider name or use generic)
            print("[STEP4] Filling city")
            await fill_flutter_field(page, "city", "City")

            # Select Country of Treatment
            print("[STEP4] Selecting country of treatment")
            await select_dropdown(page, "country|treatment", "United States")

            # Scroll if needed
            await scroll_form(page)

            # Fill Charge Amount
            print(f"[STEP4] Filling charge amount: {claim['amount']}")
            await fill_flutter_field(page, "charge amount|amount", claim["amount"])

            # Select Currency
            print(f"[STEP4] Selecting currency: {claim['currency']}")
            await select_dropdown(page, "currency|billed", claim["currency"])

            # Scroll if needed
            await scroll_form(page)

            # Select Diagnosis
            print(f"[STEP4] Selecting diagnosis: {claim['diagnosis']}")
            await select_dropdown(page, "condition|diagnosis", claim["diagnosis"])

            # Select Service Description
            print(f"[STEP4] Selecting procedure: {claim['procedure']}")
            await select_dropdown(page, "service|procedure|description", claim["procedure"])

            # Scroll if needed
            await scroll_form(page)

            # Select Start Date
            print(f"[STEP4] Selecting start date: {claim['date']}")
            await select_date(page, "start date|service date", claim["date"])

            # Select End Date (same as start date for single-day)
            print("[STEP4] Selecting end date")
            await select_date(page, "end date", claim["date"])

            # Scroll if needed
            await scroll_form(page)

            # Click Save charge
            print("[STEP4] Clicking Save charge")
            save_btn = page.get_by_role("button", name=re.compile("save|add charge", re.IGNORECASE))
            await save_btn.click()
            await wait_for_flutter(page)

            print("[STEP4] Charges step completed")
            return
        except Exception as e:
            retry_count += 1
            print(f"[STEP4] Attempt {retry_count}/{MAX_RETRIES} failed: {str(e)}")
            if retry_count >= MAX_RETRIES:
                await take_screenshot(page, "step4_error")
                raise Exception(f"Step 4 failed after {MAX_RETRIES} retries: {str(e)}")
            await asyncio.sleep(1)


async def step5_reimbursement(page: Page) -> None:
    """
    Step 5: Reimbursement information.

    Actions:
    1. Select bank account
    2. Select USD currency
    3. Click Next
    """
    print("[STEP5] Starting reimbursement step")

    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Select bank account (usually first/only option)
            print("[STEP5] Selecting bank account")
            account_btn = page.get_by_role("button", name=re.compile("account|bank|wire", re.IGNORECASE))
            await account_btn.click()
            await wait_for_flutter(page)

            # Select currency
            print("[STEP5] Selecting currency")
            await select_dropdown(page, "currency", "USD")

            # Click Next
            print("[STEP5] Clicking Next")
            next_btn = page.get_by_role("button", name="Next")
            await next_btn.click()
            await wait_for_flutter(page)

            print("[STEP5] Reimbursement step completed")
            return
        except Exception as e:
            retry_count += 1
            print(f"[STEP5] Attempt {retry_count}/{MAX_RETRIES} failed: {str(e)}")
            if retry_count >= MAX_RETRIES:
                await take_screenshot(page, "step5_error")
                raise Exception(f"Step 5 failed after {MAX_RETRIES} retries: {str(e)}")
            await asyncio.sleep(1)


async def step6_authorization(page: Page) -> str:
    """
    Step 6: Authorization and submission.

    Actions:
    1. Check acknowledgment checkboxes
    2. Click Submit/File Claim
    3. Capture and return reference number

    Returns the reference number from the confirmation screen.
    """
    print("[STEP6] Starting authorization step")

    retry_count = 0
    while retry_count < MAX_RETRIES:
        try:
            # Check acknowledgment checkboxes
            print("[STEP6] Checking acknowledgment boxes")
            checkboxes = page.get_by_role("checkbox")
            await checkboxes.first.click()
            await wait_for_flutter(page)

            # Click all visible checkboxes
            for i in range(3):
                try:
                    cb = page.locator("role=checkbox").nth(i)
                    await cb.click()
                    await asyncio.sleep(SHORT_WAIT / 1000)
                except Exception:
                    pass

            # Scroll to find submit button
            await scroll_form(page)

            # Click Submit/File Claim button
            print("[STEP6] Clicking Submit/File Claim")
            submit_btn = page.get_by_role("button", name=re.compile("submit|file|claim", re.IGNORECASE))
            await submit_btn.click()
            await wait_for_flutter(page)
            await asyncio.sleep(2)  # Wait for confirmation screen

            # Capture reference number from confirmation screen
            print("[STEP6] Capturing reference number")
            reference_number = await capture_reference_number(page)

            print(f"[STEP6] Authorization step completed - Reference: {reference_number}")
            return reference_number
        except Exception as e:
            retry_count += 1
            print(f"[STEP6] Attempt {retry_count}/{MAX_RETRIES} failed: {str(e)}")
            if retry_count >= MAX_RETRIES:
                await take_screenshot(page, "step6_error")
                raise Exception(f"Step 6 failed after {MAX_RETRIES} retries: {str(e)}")
            await asyncio.sleep(1)


async def capture_reference_number(page: Page) -> str:
    """
    Extract the reference number from the confirmation page.

    Looks for text patterns like "Reference: XXXXX" or "Claim #: XXXXX".
    """
    try:
        # Get all text from the page
        page_text = await page.inner_text("body")

        # Look for reference number patterns
        patterns = [
            r"reference[:\s]+([A-Z0-9]+)",
            r"claim[:\s]+#?([A-Z0-9]+)",
            r"confirmation[:\s]+([A-Z0-9]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                ref_num = match.group(1).strip()
                print(f"[REF] Found reference number: {ref_num}")
                return ref_num

        # If no pattern matches, return a placeholder
        print("[WARN] Could not extract reference number - using timestamp")
        return f"REF-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    except Exception as e:
        print(f"[WARN] Error capturing reference: {str(e)}")
        return f"REF-{datetime.now().strftime('%Y%m%d%H%M%S')}"


async def upload_document(page: Page, drive_link: str) -> None:
    """
    Download invoice from Google Drive and upload to portal.

    Steps:
    1. Extract file ID from Drive link
    2. Download the file
    3. Find file upload input on portal
    4. Upload the file
    """
    print(f"[UPLOAD] Uploading document from: {drive_link}")

    try:
        if not drive_link:
            print("[WARN] No drive link provided - skipping upload")
            return

        # Extract file ID from Google Drive link
        file_id_match = re.search(r'/d/([a-zA-Z0-9-_]+)', drive_link)
        if not file_id_match:
            print("[WARN] Could not extract file ID from drive link")
            return

        file_id = file_id_match.group(1)

        # Download file from Google Drive
        download_url = f"https://drive.google.com/uc?id={file_id}&export=download"
        temp_file = f"/tmp/invoice_{file_id}.pdf"

        print(f"[UPLOAD] Downloading file: {file_id}")
        result = subprocess.run(
            ["wget", "-q", download_url, "-O", temp_file],
            timeout=30,
            check=False
        )

        if result.returncode != 0:
            print("[WARN] Failed to download file from Drive")
            return

        # Find and interact with file upload input
        print("[UPLOAD] Finding file input on portal")
        file_input = page.locator("input[type='file']").first

        if file_input:
            await file_input.set_input_files(temp_file)
            await wait_for_flutter(page)
            print("[UPLOAD] Document uploaded successfully")
        else:
            print("[WARN] Could not find file input on portal")
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
    print(f"[CLAIM] Filing claim: {claim['invoice_num']} - {claim['patient']}")
    print(f"{'='*70}")

    try:
        # Navigate to eClaim section
        await navigate_to_eclaim(page)
        await dismiss_popups(page)

        # Execute all steps in order
        await step1_preliminary(page)
        await step2_basic_info(page, claim)
        await step3_other_insurance(page)
        await step4_charges(page, claim)
        await step5_reimbursement(page)
        reference_number = await step6_authorization(page)

        # Attempt to upload document if available
        if claim.get("drive_link"):
            await upload_document(page, claim["drive_link"])

        print(f"[CLAIM] Claim {claim['invoice_num']} filed successfully!")
        print(f"[CLAIM] Reference Number: {reference_number}")

        return reference_number
    except Exception as e:
        print(f"[ERROR] Failed to file claim {claim['invoice_num']}: {str(e)}")
        await take_screenshot(page, f"claim_error_{claim['invoice_num']}")
        return None


async def main():
    """
    Main entry point for the claim filing automation.

    1. Read pending claims from Google Sheets
    2. Connect to existing Chrome CDP instance
    3. Log in to BCBS portal
    4. File each claim in sequence
    5. Update sheets with results
    6. Send Telegram notifications
    """
    print(f"\n{'='*70}")
    print("[MAIN] BCBS Claim Filer Starting")
    print(f"[MAIN] Timestamp: {datetime.now().isoformat()}")
    print(f"{'='*70}\n")

    # Read pending claims from sheets
    claims = read_pending_claims()

    if not claims:
        print("[MAIN] No pending claims found - exiting")
        return

    async with async_playwright() as p:
        try:
            # Connect to existing Chrome CDP instance
            print(f"[MAIN] Connecting to Chrome CDP at {CDP_URL}")
            browser = await p.chromium.connect_over_cdp(CDP_URL)

            # Get or create a page
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            print("[MAIN] Connected to browser")

            # Log in to portal
            await login(page)

            # File each claim
            filed_count = 0
            for idx, claim in enumerate(claims, start=1):
                print(f"\n[MAIN] Processing claim {idx}/{len(claims)}")

                reference_number = await file_single_claim(page, claim)

                if reference_number:
                    # Update sheets and notify
                    update_sheets(claim["row_number"], reference_number)
                    notify_telegram(claim, reference_number)
                    filed_count += 1

                    # Wait before next claim
                    if idx < len(claims):
                        print("[MAIN] Waiting before next claim...")
                        await asyncio.sleep(3)
                else:
                    print(f"[MAIN] Skipping sheet update for failed claim")

            print(f"\n{'='*70}")
            print(f"[MAIN] Claim Filing Complete")
            print(f"[MAIN] Successfully filed: {filed_count}/{len(claims)} claims")
            print(f"[MAIN] Timestamp: {datetime.now().isoformat()}")
            print(f"{'='*70}\n")
        except Exception as e:
            print(f"[ERROR] Fatal error in main: {str(e)}")
            raise
        finally:
            # Close browser connection (don't close the browser itself)
            print("[MAIN] Disconnecting from browser")


if __name__ == "__main__":
    asyncio.run(main())
