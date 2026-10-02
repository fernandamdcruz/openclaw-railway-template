#!/usr/bin/env python3
"""
BCBS Global Solutions — Direct API Claim Filer
================================================
Files medical claims via the BCBS/GeoBlue REST API (claimsapire.hthworldwide.com).

Uses Playwright to log in via Okta SSO (with 2FA) to obtain an OAuth token,
then files claims via authenticated REST API calls.

API Flow:
  1. POST /v4/claimants/save/       → Create claim + set patient (returns ClaimSubmissionID)
  2. POST /v4/insurance/save/        → Set other insurance (none)
  3. POST /v4/charges/save/          → Add charge (provider, diagnosis, amount, dates)
  4. POST /v4/chargedocuments/Initiate → Get S3 presigned URL
  5. PUT  <S3 URL>                   → Upload supporting document
  6. POST /v4/chargedocuments/Complete → Confirm upload
  7. POST /v4/paymentaccounts/save/  → Set payment method (saved wire account)
  8. POST /v4/claims/submit          → Submit claim with signature

Usage (from FerdyBot skill):
  python3 claim_filer_api.py

Environment variables:
  GOOGLE_SHEET_ID       — Google Sheet with claims data
  GOOGLE_SHEET_TAB      — Tab name (default: current year, e.g. "2026")
  TELEGRAM_BOT_TOKEN    — For sending result notifications
  TELEGRAM_CHAT_ID      — Chat to notify
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("[FATAL] 'requests' not installed. Fix the Dockerfile: pip install --break-system-packages requests")
    sys.exit(1)

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_VERSION = "api-v10-verify-dates-diagnosis-2026-10-02"
print(f"[INIT] BCBS API Claim Filer {SCRIPT_VERSION} initialized at {datetime.now().isoformat()}")

API_BASE = "https://claimsapire.hthworldwide.com/v4"
GEOBLUE_API = "https://geoblueapire.hthworldwide.com/v4"

# Common headers for all API calls
API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://members.bcbsglobalsolutions.com",
    "Referer": "https://members.bcbsglobalsolutions.com/",
}

# Account identity (from HAR capture)
USER_ID = 240216564258281
PEOPLE_ID = "502968557"
SITE_ID = 30

# Family members: name → (DependentID, Sequence)
FAMILY_MEMBERS = {
    "max": (None, "00"),           # Subscriber (Max Jacobson)
    "max jacobson": (None, "00"),
    "elena": (5000299525, "01"),    # Elena Jacobson (child)
    "elena jacobson": (5000299525, "01"),
    "mathias": (5000299526, "02"),  # Mathias Jacobson (child)
    "mathias jacobson": (5000299526, "02"),
    "fernanda": (5000299527, "03"), # Fernanda Miranda da Cruz (spouse)
    "fernanda miranda": (5000299527, "03"),
    "fernanda miranda da cruz": (5000299527, "03"),
}

# Default claimant contact info
DEFAULT_CLAIMANT = {
    "PhoneNumber": "+5511912228841",
    "EmailAddress": "fernanda.mdcruz@gmail.com",
    "EmployerName": "max",
    "Address": {
        "Country": "United States",
        "CityLocale": "Chalfont",
        "StateProvince": "Pennsylvania",
        "StreetAddress1": "11 Deerpath Road",
        "StreetAddress2": None,
        "PostalCode": "18914"
    }
}

# Saved payment account (wire transfer)
SAVED_PAYMENT_ACCOUNT = {
    "Name": "*****4135",
    "PaymentAccountID": 141210,
    "BankName": None,
    "CountryID": 202,
    "OriginalStateProvince": None,
    "CurrencyID": 27,
    "AbaSwift": "321081669",
    "AccountNumber": " 80006224135",
    "SortCode": None,
    "BankIban": None,
    "IntermediateBankName": None,
    "IntermediateAbaNumber": None,
    "IntermediateAccountNumber": "",
    "IsIbanValid": None,
    "IsSaved": True
}

# Country name → CountryID mapping
COUNTRY_IDS = {
    "austria": 11, "brazil": 24, "canada": 31, "france": 63,
    "germany": 68, "italy": 90, "japan": 93, "mexico": 117,
    "portugal": 144, "spain": 162, "switzerland": 174,
    "united kingdom": 972, "uk": 972, "united states": 202, "us": 202, "usa": 202,
}

# Currency name → CurrencyID mapping
CURRENCY_IDS = {
    "aud": 1, "australian dollar": 1,
    "gbp": 2, "british pound": 2, "pound": 2,
    "cad": 3, "canadian dollar": 3,
    "eur": 6, "euro": 6,
    "jpy": 11, "japanese yen": 11, "yen": 11,
    "chf": 24, "swiss franc": 24,
    "usd": 27, "us dollar": 27, "dollar": 27,
    "brl": 220, "brazilian real": 220, "real": 220, "reais": 220,
}

# Country → default currency
COUNTRY_CURRENCY = {
    11: 6,    # Austria → EUR
    24: 220,  # Brazil → BRL
    31: 3,    # Canada → CAD
    63: 6,    # France → EUR
    68: 6,    # Germany → EUR
    90: 6,    # Italy → EUR
    93: 11,   # Japan → JPY
    117: 27,  # Mexico → USD (commonly billed in USD)
    144: 6,   # Portugal → EUR
    162: 6,   # Spain → EUR
    174: 24,  # Switzerland → CHF
    972: 2,   # UK → GBP
    202: 27,  # US → USD
}

# ── Dynamic diagnosis & service caches (fetched from API at runtime) ──
# Populated by fetch_diagnosis_options() and fetch_service_options()
_AVAILABLE_DIAGNOSES: List[Dict] = []   # [{Icd10, Description}, ...]
_AVAILABLE_SERVICES: List[Dict] = []    # [{Value, Name}, ...]

# Fallback keyword → ICD10 mapping (used when API fetch fails or no match found)
DIAGNOSIS_KEYWORD_FALLBACK = {
    "acne": ("L700", "OTHER ACNE"),
    "rash": ("R21", "RASH OR SKIN IRRITATION"),
    "skin": ("R21", "RASH OR SKIN IRRITATION"),
    "dermatology": ("R21", "RASH OR SKIN IRRITATION"),
    "lesion": ("R21", "RASH OR SKIN IRRITATION"),
    "respiratory": ("J069", "UPPER RESPIRATORY INFECTION"),
    "cold": ("J069", "UPPER RESPIRATORY INFECTION"),
    "flu": ("J069", "UPPER RESPIRATORY INFECTION"),
    "uti": ("N390", "URINARY TRACT INFECTION"),
    "urinary": ("N390", "URINARY TRACT INFECTION"),
    "stomach": ("R109", "ABDOMINAL OR STOMACH PAIN"),
    "abdominal": ("R109", "ABDOMINAL OR STOMACH PAIN"),
    "food poisoning": ("A059", "FOOD POISONING"),
    "chest pain": ("R079", "CHEST PAIN"),
    "heart": ("I219", "HEART ATTACK"),
    "back pain": ("M5440", "LOWER BACK PAIN"),
    "lower back": ("M5440", "LOWER BACK PAIN"),
    "anxiety": ("F418", "ANXIETY DISORDER"),
    "routine": ("Z0000", "ROUTINE MEDICAL EXAM HEALTH FACIL"),
    "checkup": ("Z0000", "ROUTINE MEDICAL EXAM HEALTH FACIL"),
    "check-up": ("Z0000", "ROUTINE MEDICAL EXAM HEALTH FACIL"),
    "preventive": ("Z0000", "ROUTINE MEDICAL EXAM HEALTH FACIL"),
    "physical": ("Z0000", "ROUTINE MEDICAL EXAM HEALTH FACIL"),
    "wellness": ("Z0000", "ROUTINE MEDICAL EXAM HEALTH FACIL"),
    "ankle": ("S99919A", "UNSPECIFIED INJURY OF UNSPECIFIED ANKLE, INITIAL ENCOUNTER"),
    "dental": ("K029", "DENTAL CARIES"),
    "vision": ("H539", "VISUAL DISTURBANCE"),
    "eye": ("H539", "VISUAL DISTURBANCE"),
    "other": ("ECLAIM", "OTHER"),
}

# Fallback keyword → service description (used when no dynamic match)
SERVICE_KEYWORD_FALLBACK = {
    "office": "Office Consultation",
    "consultation": "Office Consultation",
    "doctor": "Office Consultation",
    "visit": "Office Consultation",
    "wellness": "Wellness Physical Exam",
    "physical exam": "Wellness Physical Exam",
    "lab": "Laboratory or Diagnostic Testing",
    "laboratory": "Laboratory or Diagnostic Testing",
    "test": "Laboratory or Diagnostic Testing",
    "blood": "Laboratory or Diagnostic Testing",
    "vaccine": "Laboratory Testing and/or Vaccinations",
    "vaccination": "Laboratory Testing and/or Vaccinations",
    "surgery": "Inpatient or Outpatient Surgical Services",
    "dental": "Dental Exam and Cleaning",
    "vision": "Vision Exam and/or Glasses/Contacts",
    "glasses": "Vision Exam and/or Glasses/Contacts",
    "therapy": "Counseling or Therapy visits",
    "counseling": "Counseling or Therapy visits",
    "emergency": "Emergency Room",
    "hospital": "Inpatient Hospital Admission",
}

# Google Sheets config — hardcoded defaults so it works without env vars
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk")
# Tab is named after the year (one tab per year). Derive it so this keeps
# working after Jan 1 without a code change; override with GOOGLE_SHEET_TAB.
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", str(datetime.now().year))

# gog CLI environment — hardcode ALL required vars
GOG_ENV = os.environ.copy()
GOG_ENV["GOG_CONFIG_DIR"] = os.environ.get("GOG_CONFIG_DIR", "/data/workspace/.config")
GOG_ENV["XDG_CONFIG_HOME"] = os.environ.get("XDG_CONFIG_HOME", "/data/workspace/.config")
GOG_ENV["GOG_ACCOUNT"] = os.environ.get("GOG_ACCOUNT", "fernanda.mdcruz@gmail.com")
GOG_ENV["GOG_KEYRING_PASSWORD"] = os.environ.get("GOG_KEYRING_PASSWORD", "ferdybot-calendar-2026")


# ============================================================================
# OAUTH LOGIN (Playwright-based, only if API requires auth)
# ============================================================================

# Okta PKCE OAuth config (from HAR capture)
OKTA_TOKEN_ENDPOINT = "https://login.members.bcbsglobalsolutions.com/oauth2/ausdd4gjt9swXP2Uv4h7/v1/token"
OKTA_CLIENT_ID = "0oaddmkwyk7EHdc9j4h7"


def _manual_token_exchange(auth_code: str, callback_url: str) -> Optional[str]:
    """
    Exchange an OAuth authorization code for an access token manually.
    This bypasses the browser's client-side token exchange which Playwright may miss.
    """
    from urllib.parse import urlparse, parse_qs

    # Extract the redirect_uri from the callback URL (strip the query params)
    parsed = urlparse(callback_url)
    redirect_uri = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    print(f"[AUTH] Manual token exchange: code length={len(auth_code)}, redirect_uri={redirect_uri}")

    try:
        resp = requests.post(
            OKTA_TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": OKTA_CLIENT_ID,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        print(f"[AUTH] Token exchange response: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("access_token")
            if token:
                print(f"[AUTH] Token obtained via manual exchange (expires_in={data.get('expires_in')}s)")
                return token
            else:
                print(f"[AUTH] Token exchange succeeded but no access_token in response: {list(data.keys())}")
        else:
            print(f"[AUTH] Token exchange failed: {resp.text[:300]}")
    except Exception as e:
        print(f"[AUTH] Token exchange error: {e}")

    return None


async def obtain_oauth_token() -> Optional[str]:
    """
    Use Playwright to log in to BCBS via Okta SSO and intercept the OAuth
    access_token from the /v1/token response. Handles 2FA via Gmail.
    Returns the Bearer token string, or None on failure.
    """
    import asyncio

    username = os.environ.get("BCBS_USERNAME")
    password = os.environ.get("BCBS_PASSWORD")
    if not username or not password:
        print("[AUTH] No BCBS_USERNAME/BCBS_PASSWORD env vars — cannot obtain token")
        return None

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[AUTH] Playwright not installed — cannot obtain token")
        return None

    captured_token = {"value": None}
    captured_auth_code = {"value": None, "url": None}

    async def intercept_token(response):
        """Capture the access_token from Okta's /v1/token response."""
        if "/v1/token" in response.url and response.status == 200:
            try:
                data = await response.json()
                token = data.get("access_token")
                if token:
                    captured_token["value"] = token
                    print(f"[AUTH] Captured OAuth token via response listener (expires_in={data.get('expires_in')}s)")
            except Exception as e:
                print(f"[AUTH] Failed to parse token response: {e}")

    async def intercept_callback(request):
        """Capture the authorization code from the OAuth callback redirect."""
        url = request.url
        if "code=" in url and ("callback" in url or "redirect" in url or "bcbsglobalsolutions" in url):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if code:
                captured_auth_code["value"] = code
                captured_auth_code["url"] = url
                print(f"[AUTH] Captured OAuth authorization code from callback (length: {len(code)})")

    print("[AUTH] Starting Playwright login to obtain OAuth token...")
    screenshot_dir = "/tmp/bcbs_auth_screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)

    async def _screenshot(page, name):
        """Save a debug screenshot and log the path."""
        path = f"{screenshot_dir}/{name}_{datetime.now().strftime('%H%M%S')}.png"
        try:
            await page.screenshot(path=path)
            print(f"[AUTH] Screenshot saved: {path}")
        except Exception as e:
            print(f"[AUTH] Screenshot failed: {e}")

    async def _dump_page_state(page, label):
        """Log current URL, title, and visible text for debugging."""
        try:
            url = page.url
            title = await page.title()
            text = (await page.text_content("body") or "")[:500]
            print(f"[AUTH] [{label}] URL: {url}")
            print(f"[AUTH] [{label}] Title: {title}")
            print(f"[AUTH] [{label}] Body text (first 500 chars): {text}")
        except Exception as e:
            print(f"[AUTH] [{label}] Could not dump page state: {e}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        # Listen for the token response AND the callback redirect
        page.on("response", intercept_token)
        page.on("request", intercept_callback)

        try:
            # Navigate to login
            portal_url = "https://members.bcbsglobalsolutions.com"
            print(f"[AUTH] Navigating to {portal_url}")
            await page.goto(portal_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(5)
            await _dump_page_state(page, "after-landing")
            await _screenshot(page, "01_landing")

            # Click Login button (Flutter landing page — may match multiple)
            login_btn = page.get_by_role("button", name=re.compile("^login$", re.IGNORECASE))
            btn_count = await login_btn.count()
            if btn_count > 0:
                print(f"[AUTH] Found {btn_count} Login button(s), clicking first")
                await login_btn.first.click()
                await asyncio.sleep(5)
                print(f"[AUTH] Clicked Login button, redirected to: {page.url}")
            else:
                # Try alternative selectors — the login element may not be a button
                print(f"[AUTH] No 'Login' button found (count=0). Trying alternative selectors...")
                alt_selectors = [
                    page.get_by_role("link", name=re.compile("login|sign.in|log.in", re.IGNORECASE)),
                    page.locator("a[href*='login'], a[href*='signin'], a[href*='auth']"),
                    page.locator("text=/login/i"),
                ]
                clicked = False
                for i, alt in enumerate(alt_selectors):
                    alt_count = await alt.count()
                    print(f"[AUTH]   Alternative selector {i}: found {alt_count} match(es)")
                    if alt_count > 0:
                        await alt.first.click()
                        await asyncio.sleep(5)
                        print(f"[AUTH]   Clicked alternative selector {i}, now at: {page.url}")
                        clicked = True
                        break
                if not clicked:
                    print("[AUTH] WARNING: Could not find any login button/link on landing page")
                    await _dump_page_state(page, "no-login-btn")
                    await _screenshot(page, "02_no_login_btn")

            await _dump_page_state(page, "before-username")
            await _screenshot(page, "03_before_username")

            # Fill username
            username_input = page.locator('input[name="identifier"]')
            await username_input.wait_for(state="visible", timeout=15000)
            await username_input.fill(username)
            print("[AUTH] Username entered")

            # Fill password
            password_input = page.locator('input[name="credentials.passcode"]')
            await password_input.wait_for(state="visible", timeout=5000)
            await password_input.fill(password)
            print("[AUTH] Password entered")
            await _screenshot(page, "04_credentials_filled")

            # Submit login
            import time as _time
            login_epoch = int(_time.time())
            submit_btn = page.locator('input[type="submit"][value="SIGN IN"]')
            if await submit_btn.count() == 0:
                print("[AUTH] WARNING: 'SIGN IN' submit button not found, trying generic submit")
                submit_btn = page.locator('input[type="submit"]')
            await submit_btn.click()
            print(f"[AUTH] SIGN IN clicked (epoch: {login_epoch})")
            await asyncio.sleep(5)
            await _dump_page_state(page, "after-sign-in")
            await _screenshot(page, "05_after_sign_in")

            # Check for 2FA
            current_url = page.url
            page_text = await page.text_content("body") or ""

            if "verification" in page_text.lower() or "code" in page_text.lower() or "factor" in page_text.lower():
                print("[AUTH] 2FA detected — asking user for code via Telegram")
                await _screenshot(page, "06_2fa_prompt")

                # Ask the user directly via Telegram
                code = ask_telegram_for_2fa()

                if code:
                    print(f"[AUTH] Got 2FA code: ****{code[-2:]}")
                    # Find the verification code input
                    code_input = page.locator('input[name="credentials.passcode"]')
                    if await code_input.count() == 0:
                        code_input = page.locator('input[type="tel"]')
                    if await code_input.count() == 0:
                        code_input = page.get_by_role("textbox")

                    await code_input.first.fill(code)
                    verify_btn = page.locator('input[type="submit"]')
                    await verify_btn.click()
                    print("[AUTH] 2FA code submitted")
                    await asyncio.sleep(8)

                    # Handle "Keep me signed in" interstitial (Okta post-2FA)
                    page_text_post_2fa = await page.text_content("body") or ""
                    if "keep me signed in" in page_text_post_2fa.lower() or "stay signed in" in page_text_post_2fa.lower():
                        print("[AUTH] 'Keep me signed in' interstitial detected — clicking through")
                        await _screenshot(page, "07_keep_signed_in")
                        # Try various button/link selectors for the interstitial
                        for label, el in [
                            ("Don't stay (link)", page.get_by_role("link", name=re.compile("don.*stay signed in", re.IGNORECASE))),
                            ("Don't stay (button)", page.get_by_role("button", name=re.compile("don.*stay signed in", re.IGNORECASE))),
                            ("Stay (link)", page.get_by_role("link", name=re.compile("^stay signed in$", re.IGNORECASE))),
                            ("Stay (button)", page.get_by_role("button", name=re.compile("^stay signed in$", re.IGNORECASE))),
                            ("No (button)", page.get_by_role("button", name=re.compile("^no$", re.IGNORECASE))),
                            ("Submit", page.locator('input[type="submit"]')),
                        ]:
                            if await el.count() > 0:
                                await el.first.click()
                                print(f"[AUTH] Clicked '{label}' on interstitial")
                                await asyncio.sleep(3)
                                break
                else:
                    print("[AUTH] Could not get 2FA code — aborting")
                    return None

            # Wait for redirect and token capture
            # Give it up to 15s total, checking every second
            for wait_i in range(15):
                await asyncio.sleep(1)
                if captured_token["value"]:
                    break
            print(f"[AUTH] Final URL: {page.url}")

            # METHOD 1: Response listener caught /v1/token
            if captured_token["value"]:
                print("[AUTH] Token obtained via response listener")
                return captured_token["value"]

            # METHOD 2: We captured the auth code from the callback URL — do token exchange manually
            if captured_auth_code["value"]:
                print("[AUTH] Attempting manual token exchange with captured auth code...")
                token = _manual_token_exchange(captured_auth_code["value"], captured_auth_code["url"])
                if token:
                    return token

            # METHOD 3: Check if the current URL has a code= parameter
            current_url = page.url
            if "code=" in current_url:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(current_url)
                params = parse_qs(parsed.query)
                auth_code = params.get("code", [None])[0]
                if auth_code:
                    print(f"[AUTH] Found auth code in current URL — attempting manual exchange...")
                    token = _manual_token_exchange(auth_code, current_url)
                    if token:
                        return token

            # METHOD 4: Try to extract token from browser storage
            try:
                token_from_storage = await page.evaluate("""() => {
                    // Check localStorage
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        const val = localStorage.getItem(key);
                        if (val && val.length > 20 && (key.includes('token') || key.includes('auth') || key.includes('okta'))) {
                            try {
                                const parsed = JSON.parse(val);
                                if (parsed.accessToken) return parsed.accessToken;
                                if (parsed.access_token) return parsed.access_token;
                            } catch(e) {}
                            if (val.startsWith('eyJ')) return val;  // JWT
                        }
                    }
                    // Check sessionStorage
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const key = sessionStorage.key(i);
                        const val = sessionStorage.getItem(key);
                        if (val && val.length > 20 && (key.includes('token') || key.includes('auth') || key.includes('okta'))) {
                            try {
                                const parsed = JSON.parse(val);
                                if (parsed.accessToken) return parsed.accessToken;
                                if (parsed.access_token) return parsed.access_token;
                            } catch(e) {}
                            if (val.startsWith('eyJ')) return val;
                        }
                    }
                    return null;
                }""")
                if token_from_storage:
                    print(f"[AUTH] Token obtained from browser storage (length: {len(token_from_storage)})")
                    return token_from_storage
            except Exception as e:
                print(f"[AUTH] Browser storage check failed: {e}")

            print("[AUTH] All token capture methods failed")
            print(f"[AUTH] captured_token listener fired: {captured_token['value'] is not None}")
            print(f"[AUTH] captured_auth_code: {captured_auth_code['value'] is not None}")
            return None

        except Exception as e:
            print(f"[AUTH] Login error: {e}")
            traceback.print_exc()
            await _dump_page_state(page, "error")
            await _screenshot(page, "99_error")
            return None
        finally:
            await browser.close()


# ============================================================================
# API CLIENT
# ============================================================================

session = requests.Session()
session.headers.update(API_HEADERS)


def set_auth_token(token: str) -> None:
    """Set the Bearer token on the session for all subsequent API calls."""
    session.headers["Authorization"] = f"Bearer {token}"
    print(f"[API] Authorization header set (token length: {len(token)})")


def api_post(endpoint: str, body: dict, base: str = API_BASE) -> dict:
    """POST to the claims API and return parsed JSON response."""
    url = f"{base}{endpoint}"
    print(f"[API] POST {url}")
    print(f"[API] Body: {json.dumps(body)[:500]}")

    resp = session.post(url, json=body, timeout=30)
    print(f"[API] Status: {resp.status_code}")

    if resp.status_code not in (200, 201):
        print(f"[API] Error response: {resp.text[:500]}")
        raise Exception(f"API error {resp.status_code}: {resp.text[:200]}")

    if not resp.text.strip():
        return {}

    data = resp.json()
    print(f"[API] Response: {json.dumps(data)[:500]}")
    return data


def api_get(endpoint: str, params: dict = None, base: str = API_BASE) -> Any:
    """GET from the claims API and return parsed JSON response."""
    url = f"{base}{endpoint}"
    print(f"[API] GET {url}")

    resp = session.get(url, params=params, timeout=30)
    print(f"[API] Status: {resp.status_code}")

    if resp.status_code != 200:
        print(f"[API] Error response: {resp.text[:500]}")
        raise Exception(f"API error {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    print(f"[API] Response: {json.dumps(data) if isinstance(data, (list,)) and len(json.dumps(data)) < 300 else json.dumps(data)[:500]}")
    return data


# ============================================================================
# DYNAMIC REFERENCE DATA (fetched from API)
# ============================================================================

def fetch_diagnosis_options(sequence: str) -> List[Dict]:
    """
    Fetch available diagnosis options from the API.
    Calls GetMemberAllAssessments which returns:
      - Member-specific past diagnoses
      - Generic common diagnoses
    Both lists are combined into a single flat list of {Icd10, Description}.
    """
    global _AVAILABLE_DIAGNOSES
    if _AVAILABLE_DIAGNOSES:
        return _AVAILABLE_DIAGNOSES

    try:
        import uuid
        body = {
            "HTTPRequestID": str(uuid.uuid4()),
            "CertificateNo": PEOPLE_ID,
            "Sequence": sequence,
            "Product": "TRAVEL GAP"
        }
        resp = api_post("/actisure/GetMemberAllAssessments", body)

        combined = resp.get("CombinedAssessments", {})
        member_list = combined.get("Member", {}).get("Assessment", [])
        generic_list = combined.get("GenericAssessments", {}).get("Assessment", [])

        _AVAILABLE_DIAGNOSES = member_list + generic_list
        print(f"[REF] Loaded {len(member_list)} member + {len(generic_list)} generic diagnoses")
        for d in _AVAILABLE_DIAGNOSES:
            print(f"[REF]   {d['Icd10']:10s} = {d['Description']}")
        return _AVAILABLE_DIAGNOSES

    except Exception as e:
        print(f"[REF] Failed to fetch diagnoses: {e}")
        return []


def fetch_service_options() -> List[Dict]:
    """
    Fetch available service descriptions from the API.
    Returns both ProviderServices (for Doctor) and FacilityServices.
    """
    global _AVAILABLE_SERVICES
    if _AVAILABLE_SERVICES:
        return _AVAILABLE_SERVICES

    try:
        resp = api_get("/claims/services/providerservices")
        provider = resp.get("ProviderServices", [])
        facility = resp.get("FacilityServices", [])
        _AVAILABLE_SERVICES = provider + facility
        print(f"[REF] Loaded {len(provider)} provider + {len(facility)} facility services")
        for s in _AVAILABLE_SERVICES:
            print(f"[REF]   {s['Value']:8s} = {s['Name']}")
        return _AVAILABLE_SERVICES

    except Exception as e:
        print(f"[REF] Failed to fetch services: {e}")
        return []


def _score_text_match(query: str, candidate: str) -> int:
    """
    Score how well a query matches a candidate string.
    Higher = better match. Returns 0 for no match.
    """
    q = query.lower().strip()
    c = candidate.lower().strip()

    # Exact match
    if q == c:
        return 1000

    # Query is an ICD-10 code that matches exactly (strip dots: L70.0 → L700)
    q_code = re.sub(r'[.\s-]', '', q)
    c_code = re.sub(r'[.\s-]', '', c)
    if q_code == c_code:
        return 900

    # One contains the other
    if q in c:
        return 500 + len(q)  # Longer match = better
    if c in q:
        return 400 + len(c)

    # Word-level overlap
    q_words = set(re.findall(r'[a-z]+', q))
    c_words = set(re.findall(r'[a-z]+', c))
    overlap = q_words & c_words
    # Remove trivially common words
    overlap -= {"the", "a", "an", "of", "or", "and", "for", "in", "on", "to", "is"}
    if overlap:
        return 100 + len(overlap) * 50

    return 0


# ============================================================================
# CLAIM BUILDING HELPERS
# ============================================================================

def make_claim_object(claim_submission_id: Optional[int] = None) -> dict:
    """Build the standard Claim object used in most API calls."""
    today_str = datetime.now().strftime("%d-%b-%Y").upper()
    return {
        "ClaimSubmissionID": claim_submission_id,
        "ApplicationType": "GeoBlue",
        "SourceType": "Mobile",
        "UserID": USER_ID,
        "EntryType": "APPLICATION",
        "PayeeType": "INSURED",
        "Name": f"CLM {today_str}",
        "PeopleID": PEOPLE_ID,
        "HasOtherInsurance": False,
        "IsAccident": False,
        "IsSportsInjury": False,
        "PaymentMethod": "WIRE"
    }


def resolve_patient(patient_name: str) -> Tuple[Optional[int], str]:
    """Resolve patient name to (DependentID, Sequence)."""
    key = patient_name.strip().lower()
    if key in FAMILY_MEMBERS:
        return FAMILY_MEMBERS[key]

    # Fuzzy match: check if any key is contained in the input
    for name, ids in FAMILY_MEMBERS.items():
        if name in key or key in name:
            return ids

    # Default to Fernanda if ambiguous
    print(f"[WARN] Unknown patient '{patient_name}', defaulting to Fernanda")
    return (5000299527, "03")


def resolve_country(country_name: str) -> int:
    """Resolve country name to CountryID."""
    key = country_name.strip().lower()
    if key in COUNTRY_IDS:
        return COUNTRY_IDS[key]

    # Fuzzy match
    for name, cid in COUNTRY_IDS.items():
        if name in key or key in name:
            return cid

    print(f"[WARN] Unknown country '{country_name}', defaulting to Brazil (24)")
    return 24


def resolve_currency(currency_str: str, country_id: int = None) -> int:
    """Resolve currency string to CurrencyID."""
    key = currency_str.strip().lower()
    if key in CURRENCY_IDS:
        return CURRENCY_IDS[key]

    # Try by country
    if country_id and country_id in COUNTRY_CURRENCY:
        return COUNTRY_CURRENCY[country_id]

    print(f"[WARN] Unknown currency '{currency_str}', defaulting to BRL (220)")
    return 220


# Things written into column G when a bill had no diagnosis. They are not
# diagnoses: filing them produced "OTHER" on the form and BCBS rejected the claim.
_PLACEHOLDER_DIAGNOSES = {"", "not specified on bill", "not specified", "n/a", "na",
                          "none", "unknown", "-"}

# Leading ICD-10 / CID code of column G: "Z01.4 - ...", "G473", "CID Z 01.0 ...".
# Anchored at the start because column G puts the code first; unanchored, a
# reason like "Vitamin B12 deficiency" would be read as CID B12.
_ICD10_RE = re.compile(
    r'^\s*(?:CID[\s:]*)?([A-Z])\s?(\d{2})(?:\s?\.\s?([0-9A-Z]{1,4})|([0-9A-Z]{1,4}))?\b')


def extract_icd10(text: str) -> Optional[str]:
    """Return column G's leading ICD-10 code in API form (no dot): 'Z01.4 - x' → 'Z014'."""
    m = _ICD10_RE.match(text or "")
    return (m.group(1) + m.group(2) + (m.group(3) or m.group(4) or "")) if m else None


def _norm_icd(code: str) -> str:
    return re.sub(r'[.\s-]', '', code or "").upper()


def resolve_diagnosis(diagnosis_text: str, sequence: str = "03") -> Tuple[str, str]:
    """
    Resolve column G to (ICD10Code, Description) accepted by the API.

    Column G is "code - description" (e.g. "Z01.4 - Routine gynecological
    examination"), or a plain-words reason when no document carries a CID.

    Strategy:
    1. Placeholder ("Not specified on bill", blank) → OTHER, which the caller
       refuses to file.
    2. A CID is present → use BCBS's own entry if the code is in the member's
       list, otherwise send the CID itself. Never OTHER when we have a CID.
    3. Plain-words reason → fuzzy match against BCBS's list, then whole-word
       keyword map.
    4. Nothing matched → OTHER (caller refuses to file).
    """
    text = (diagnosis_text or "").strip()
    if text.lower() in _PLACEHOLDER_DIAGNOSES:
        print(f"[DIAG] Column G has no diagnosis ({text!r})")
        return ("ECLAIM", "OTHER")

    print(f"[DIAG] Resolving diagnosis: '{text}'")

    # Fetch available options from API
    options = fetch_diagnosis_options(sequence)

    code = extract_icd10(text)
    if code:
        for opt in options:
            if _norm_icd(opt["Icd10"]) == code:
                print(f"[DIAG] Exact ICD-10 match: {opt['Icd10']} = {opt['Description']}")
                return (opt["Icd10"], opt["Description"])

        # BCBS's picker only lists the member's past diagnoses plus a short
        # generic list, so a new CID is usually missing from it. Send the real
        # CID rather than OTHER; verify_saved_charge() confirms it stuck before
        # the claim is submitted.
        desc = _ICD10_RE.sub("", text, count=1).strip(" -–—:,;").strip()
        if not re.search(r'[A-Za-zÀ-ÿ]{3,}', desc):  # e.g. a bare list of more codes
            desc = code
        print(f"[DIAG] CID {code} not in member's BCBS list — sending it directly: {desc}")
        return (code, desc.upper())

    if options:
        # ── Fuzzy match against both code AND description ──
        best_score = 0
        best_match = None
        for opt in options:
            # Score against description
            score_desc = _score_text_match(text, opt["Description"])
            # Score against code
            score_code = _score_text_match(text, opt["Icd10"])
            score = max(score_desc, score_code)
            if score > best_score:
                best_score = score
                best_match = opt

        if best_match and best_score >= 100:
            print(f"[DIAG] Fuzzy match (score={best_score}): {best_match['Icd10']} = {best_match['Description']}")
            return (best_match["Icd10"], best_match["Description"])

    # ── Keyword fallback ──
    # Whole words only. Substring matching filed "routine ..." as URINARY TRACT
    # INFECTION ("uti" is inside "routine") and "incidental" as DENTAL CARIES.
    key = text.lower()
    for keyword, (icd, desc) in DIAGNOSIS_KEYWORD_FALLBACK.items():
        if re.search(rf'\b{re.escape(keyword)}\b', key):
            print(f"[DIAG] Keyword fallback '{keyword}': {icd} = {desc}")
            return (icd, desc)

    print(f"[DIAG] No match for '{text}', using OTHER")
    return ("ECLAIM", "OTHER")


def resolve_service(diagnosis_text: str, procedure_codes: str = "",
                    bill_type: str = "", provider_type: str = "Doctor") -> str:
    """
    Resolve service description from diagnosis, procedure codes, and bill type.

    Strategy:
    1. Fetch available services from API
    2. Try fuzzy match against procedure codes / bill type / diagnosis
    3. Fall back to keyword map
    4. Default to "Office Consultation" (Doctor) or "Emergency Room" (Facility)
    """
    # Combine all available text for matching
    search_text = " ".join(filter(None, [diagnosis_text, procedure_codes, bill_type])).strip()
    if not search_text:
        return "Office Consultation" if provider_type == "Doctor" else "Emergency Room"

    print(f"[SVC] Resolving service from: '{search_text}'")

    # Fetch available options from API
    options = fetch_service_options()

    if options:
        best_score = 0
        best_match = None
        for opt in options:
            score = _score_text_match(search_text, opt["Name"])
            if score > best_score:
                best_score = score
                best_match = opt

        if best_match and best_score >= 100:
            print(f"[SVC] Fuzzy match (score={best_score}): {best_match['Name']}")
            return best_match["Name"]

    # Keyword fallback
    key = search_text.lower()
    for keyword, service in SERVICE_KEYWORD_FALLBACK.items():
        if keyword in key:
            print(f"[SVC] Keyword fallback '{keyword}': {service}")
            return service

    default = "Office Consultation" if provider_type == "Doctor" else "Emergency Room"
    print(f"[SVC] No match, defaulting to: {default}")
    return default


def format_date_api(date_str: str) -> str:
    """
    Column D (YYYY-MM-DD) → YYYYMMDD, the format the API accepts on write.

    Strict on purpose. This used to try %m/%d/%Y before %d/%m/%Y, so a
    Brazilian 05/08/2026 was silently filed as May 8. A refused claim is
    recoverable; a wrong service date is not.
    """
    s = (date_str or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    raise ValueError(f"Date of Service (column D) must be YYYY-MM-DD, got {date_str!r}")


def parse_api_date(value: Any) -> Optional[str]:
    """
    Parse a date as the claims API returns it, to YYYYMMDD.

    The API WRITES YYYYMMDD but READS BACK DD-MON-YY (e.g. "27-AUG-26"); the
    portal converts back before re-sending (see its pR()/dKl() helpers).
    """
    s = str(value or "").strip().split("T")[0].split(" ")[0]
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return None


# ============================================================================
# DOCUMENT UPLOAD
# ============================================================================

def _extract_drive_file_id(drive_link: str) -> Optional[str]:
    """Extract Google Drive file ID from various URL formats."""
    if "/d/" in drive_link:
        return drive_link.split("/d/")[1].split("/")[0].split("?")[0]
    elif "id=" in drive_link:
        return drive_link.split("id=")[1].split("&")[0]
    elif not drive_link.startswith("http"):
        return drive_link  # Assume it's already a file ID
    return None


def download_from_drive(drive_link: str, output_path: str) -> bool:
    """Download a file from Google Drive. Tries direct HTTP first, then gog CLI as fallback."""
    print(f"[DOC] Downloading from Drive: {drive_link}")

    file_id = _extract_drive_file_id(drive_link)
    if not file_id:
        print(f"[DOC] Could not extract file ID from: {drive_link}")
        return False

    print(f"[DOC] Extracted file ID: {file_id}")

    # Method 1: Direct HTTP download via Google Drive API
    # Uses the confirm=1 trick to bypass the virus scan warning for large files
    for download_url in [
        f"https://drive.google.com/uc?export=download&id={file_id}&confirm=1",
        f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
    ]:
        try:
            print(f"[DOC] Trying direct download: {download_url[:80]}...")
            resp = requests.get(download_url, timeout=60, allow_redirects=True)
            if resp.status_code == 200 and len(resp.content) > 100:
                # Check it's not an HTML error page
                if not resp.content[:50].strip().startswith(b"<!"):
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    print(f"[DOC] Direct download success: {len(resp.content)} bytes")
                    return True
                else:
                    print(f"[DOC] Got HTML instead of file (probably needs auth)")
            else:
                print(f"[DOC] Direct download failed: status={resp.status_code}, size={len(resp.content)}")
        except Exception as e:
            print(f"[DOC] Direct download error: {e}")

    # Method 2: gog CLI (may have Drive OAuth scope)
    try:
        print(f"[DOC] Trying gog drive download...")
        result = subprocess.run(
            ["gog", "drive", "download", file_id, "--out", output_path],
            capture_output=True, text=True, timeout=60, env=GOG_ENV
        )
        if result.returncode == 0:
            file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            print(f"[DOC] gog download result: {file_size} bytes")
            if file_size > 0:
                return True
        else:
            print(f"[DOC] gog download failed: {result.stderr[:200]}")
    except Exception as e:
        print(f"[DOC] gog download error: {e}")

    # Method 3: gog drive export (alternate command)
    try:
        print(f"[DOC] Trying gog drive export...")
        result = subprocess.run(
            ["gog", "drive", "export", file_id, "--out", output_path],
            capture_output=True, text=True, timeout=60, env=GOG_ENV
        )
        if result.returncode == 0:
            file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            print(f"[DOC] gog export result: {file_size} bytes")
            if file_size > 0:
                return True
        else:
            print(f"[DOC] gog export failed: {result.stderr[:200]}")
    except Exception as e:
        print(f"[DOC] gog export error: {e}")

    print(f"[DOC] All download methods failed for file_id={file_id}")
    return False


def upload_document(claim_id: int, charge_id: int, file_path: str, charge: dict) -> Optional[dict]:
    """
    Upload a supporting document to the claim.
    1. POST /chargedocuments/Initiate → get presigned S3 URL
    2. PUT to S3 → upload file
    3. POST /chargedocuments/Complete → confirm

    `charge` is the Charge body exactly as sent to charges/save (write format).
    """
    filename = os.path.basename(file_path)
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf"
    file_size = os.path.getsize(file_path)

    print(f"[DOC] Uploading {filename} ({file_size} bytes, ext={extension})")

    # Step 1: Get presigned URL
    initiate_body = {
        "fileExtension": extension,
        "claimSubmissionId": claim_id,
        "chargeId": charge_id
    }
    initiate_resp = api_post("/chargedocuments/Initiate", initiate_body)

    s3_url = initiate_resp.get("S3PresignedUrl")
    if not s3_url:
        print(f"[DOC] No presigned URL in response!")
        return None

    # Extract the S3 path (everything after the bucket domain, before the query)
    parsed = urlparse(s3_url)
    s3_path = parsed.path.lstrip("/")

    print(f"[DOC] S3 presigned URL obtained, uploading...")

    # Step 2: PUT to S3
    content_type_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif",
        "pdf": "application/pdf",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    content_type = content_type_map.get(extension, "application/octet-stream")

    with open(file_path, "rb") as f:
        file_data = f.read()

    s3_resp = requests.put(
        s3_url,
        data=file_data,
        headers={
            "Content-Type": content_type,
            "Origin": "https://members.bcbsglobalsolutions.com",
            "Referer": "https://members.bcbsglobalsolutions.com/",
        },
        timeout=120
    )

    if s3_resp.status_code != 200:
        print(f"[DOC] S3 upload failed: {s3_resp.status_code} {s3_resp.text[:200]}")
        return None

    etag = s3_resp.headers.get("ETag", "")
    print(f"[DOC] S3 upload success, ETag: {etag}")

    # Step 3: Confirm upload.
    # Complete RE-SAVES the charge from the Charge in this body, so it must be
    # in the API's write format. It used to be the charges/forclaim GET echo,
    # which reads dates back as DD-MON-YY ("27-AUG-26"); posting that back
    # wiped the service dates, leaving "Dates of Service" blank on every
    # eClaim. Send what we saved in charges/save instead — the portal does the
    # same (it converts dates back to YYYYMMDD before calling Complete).
    complete_body = {
        "Claim": make_claim_object(claim_id),
        "Charge": {**charge, "ChargeID": charge_id, "Documents": []},
        "ChargeDocument": {
            "Name": filename,
            "FileExtension": extension,
            "FileETag": etag,
            "FilePath": s3_path
        }
    }

    complete_resp = api_post("/chargedocuments/Complete", complete_body)

    doc_info = complete_resp.get("ChargeDocument", {})
    print(f"[DOC] Upload confirmed: ChargeDocumentID={doc_info.get('ChargeDocumentID')}")
    return doc_info


# ============================================================================
# VERIFICATION — the API accepts bad data silently, so check what it stored
# ============================================================================

def verify_saved_charge(claim_id: int, charge_id: int, date_api: str, icd_code: str) -> List[str]:
    """
    Before submitting: read the charge back and confirm the service dates and
    diagnosis survived every step. Returns a list of problems (empty = OK).
    """
    try:
        charges = api_get(f"/charges/forclaim/{claim_id}/")
    except Exception as e:
        return [f"could not read the charge back from BCBS ({e})"]

    saved = next((c for c in charges or [] if c.get("ChargeID") == charge_id), None)
    if not saved:
        return [f"charge {charge_id} not found on claim {claim_id}"]

    problems = []
    for field in ("ServiceStartDate", "ServiceEndDate"):
        got = parse_api_date(saved.get(field))
        if got != date_api:
            problems.append(f"{field} is {saved.get(field)!r}, expected {date_api}")

    saved_icd = _norm_icd(saved.get("ICD10Code"))
    if saved_icd in ("", "ECLAIM") or saved_icd != _norm_icd(icd_code):
        problems.append(f"diagnosis is {saved.get('ICD10Code')!r} / {saved.get('Diagnosis')!r}, "
                        f"expected {icd_code}")
    return problems


def check_eclaim_text(text: str, date_api: str, icd_code: str, diagnosis_desc: str) -> List[str]:
    """Check eClaim PDF text for the service date and a real diagnosis. Returns problems."""
    t = re.sub(r'\s+', ' ', text or "").upper()
    dt = datetime.strptime(date_api, "%Y%m%d")
    date_forms = {dt.strftime(f).upper() for f in
                  ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y",
                   "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y")}

    problems = []
    if not any(f in t for f in date_forms):
        problems.append(f"Dates of Service does not show {dt:%d/%m/%Y}")

    code = _norm_icd(icd_code)
    dotted = f"{code[:3]}.{code[3:]}" if len(code) > 3 else code
    if not any(s and s.upper() in t for s in (code, dotted, diagnosis_desc)):
        problems.append(f"Diagnosis does not show {dotted} ({diagnosis_desc})")
    return problems


def verify_eclaim_pdf(claim_id: int, date_api: str, icd_code: str, diagnosis_desc: str) -> List[str]:
    """
    After submitting: download the eClaim PDF BCBS generated (GET claims/download,
    the portal's "eClaim ID" link) and check it. Returns problems (empty = OK).
    """
    import base64
    import time

    pdf_bytes = None
    for attempt in range(4):  # the PDF can lag the submission slightly
        try:
            resp = api_get(f"/claims/download/{claim_id}/")
            content = resp.get("Content") if isinstance(resp, dict) else None
            if content:
                pdf_bytes = base64.b64decode(content) if isinstance(content, str) else bytes(content)
                break
        except Exception as e:
            print(f"[VERIFY] eClaim download attempt {attempt + 1} failed: {e}")
        time.sleep(5)
    if not pdf_bytes:
        return ["could not download the eClaim PDF to check it"]

    try:
        import fitz  # PyMuPDF, installed in the Dockerfile
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
    except Exception as e:
        return [f"could not read the eClaim PDF ({e})"]

    return check_eclaim_text(text, date_api, icd_code, diagnosis_desc)


# ============================================================================
# GOOGLE SHEETS
# ============================================================================

def read_pending_claims() -> List[Dict]:
    """Read pending claims from Google Sheet."""
    print(f"[SHEETS] Reading from sheet {GOOGLE_SHEET_ID}, tab '{GOOGLE_SHEET_TAB}'")

    result = subprocess.run(
        ["gog", "sheets", "get", GOOGLE_SHEET_ID, f"'{GOOGLE_SHEET_TAB}'!A:R", "--json"],
        capture_output=True, text=True, timeout=30, env=GOG_ENV
    )

    if result.returncode != 0:
        print(f"[SHEETS] Error: {result.stderr[:200]}")
        return []

    data = json.loads(result.stdout)
    rows = data if isinstance(data, list) else data.get("values", data.get("rows", []))

    if not rows:
        print("[SHEETS] No data found")
        return []

    # Skip header row
    claims = []
    for i, row in enumerate(rows[1:], start=2):
        """
        Column layout (updated 2026-03-27):
        A (0)  = Date Processed    B (1)  = Patient Name
        C (2)  = Provider Name     D (3)  = Date of Service
        E (4)  = Amount Billed     F (5)  = Currency
        G (6)  = Diagnosis Codes   H (7)  = Procedure Codes
        I (8)  = Invoice #         J (9)  = Year
        K (10) = City              L (11) = Country
        M (12) = Claim Status      N (13) = Drive File Link
        O (14) = Bill Type         P (15) = Secondary Doc
        Q (16) = Claim Ref #       R (17) = Notes
        """
        if len(row) <= 12:
            continue

        status = (row[12] or "").strip().lower() if len(row) > 12 else ""
        if status != "pending":
            continue

        claim = {
            "row_number": i,
            "date_processed": row[0] if len(row) > 0 else "",
            "patient_name": row[1] if len(row) > 1 else "",
            "provider_name": row[2] if len(row) > 2 else "",
            "date_of_service": row[3] if len(row) > 3 else "",
            "amount": row[4] if len(row) > 4 else "",
            "currency": row[5] if len(row) > 5 else "",
            "diagnosis": row[6] if len(row) > 6 else "",
            "procedure_codes": row[7] if len(row) > 7 else "",
            "invoice_number": row[8] if len(row) > 8 else "",
            "year": row[9] if len(row) > 9 else "",
            "city": row[10] if len(row) > 10 else "",
            "country": row[11] if len(row) > 11 else "",
            "drive_link": row[13] if len(row) > 13 else "",
            "bill_type": row[14] if len(row) > 14 else "",
            "secondary_doc": row[15] if len(row) > 15 else "",
        }

        print(f"[SHEETS] Row {i}: patient={claim['patient_name']}, provider={claim['provider_name']}, "
              f"amount={claim['amount']} {claim['currency']}, city={claim['city']}, country={claim['country']}")
        claims.append(claim)

    print(f"[SHEETS] Found {len(claims)} pending claim(s)")
    return claims


def update_sheets(row_number: int, reference_number: str, status: str = "Filed") -> None:
    """Update Google Sheet: set column M (Claim Status) and column Q (Claim Ref #)."""
    print(f"[SHEETS] Updating row {row_number}: status={status}, ref={reference_number}")

    # Update status (column M)
    subprocess.run(
        ["gog", "sheets", "update", GOOGLE_SHEET_ID,
         f"'{GOOGLE_SHEET_TAB}'!M{row_number}", status],
        capture_output=True, text=True, timeout=15, env=GOG_ENV
    )

    # Update claim ref (column Q)
    if reference_number:
        subprocess.run(
            ["gog", "sheets", "update", GOOGLE_SHEET_ID,
             f"'{GOOGLE_SHEET_TAB}'!Q{row_number}", reference_number],
            capture_output=True, text=True, timeout=15, env=GOG_ENV
        )


# ============================================================================
# TELEGRAM NOTIFICATION
# ============================================================================

def _get_telegram_creds() -> Tuple[str, str]:
    """
    Get Telegram bot token and chat ID.
    Checks env vars first, then falls back to reading the OpenClaw config file.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "8409634074")  # Fernanda's chat ID

    if not token:
        # Try reading from OpenClaw config
        config_paths = [
            "/data/.openclaw/openclaw.json",
            os.path.join(os.environ.get("OPENCLAW_STATE_DIR", ""), "openclaw.json"),
        ]
        for path in config_paths:
            try:
                with open(path, "r") as f:
                    config = json.load(f)
                # Look for Telegram bot token in channels config
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
    """Send a message to Telegram."""
    token, chat_id = _get_telegram_creds()
    if not token or not chat_id:
        print(f"[TG] No Telegram credentials, skipping notification")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10
        )
        print(f"[TG] Sent notification: {resp.status_code}")
    except Exception as e:
        print(f"[TG] Failed to send: {e}")


def _stop_openclaw_telegram_polling() -> Optional[list]:
    """
    Temporarily pause OpenClaw's Telegram polling by sending SIGSTOP to the
    gateway process. Returns the list of PIDs if stopped, or None.

    OpenClaw uses grammY's long polling (getUpdates) for Telegram. While it's
    running, it races with our own getUpdates calls and consumes messages before
    we can see them. SIGSTOP freezes the process without killing it; SIGCONT
    resumes it.
    """
    import signal

    try:
        result = subprocess.run(
            ["pgrep", "-f", "openclaw.*gateway"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = [int(p.strip()) for p in result.stdout.strip().split("\n")]
            for pid in pids:
                os.kill(pid, signal.SIGSTOP)
                print(f"[2FA] Paused OpenClaw gateway process (PID {pid})")
            return pids
    except Exception as e:
        print(f"[2FA] Could not pause gateway: {e}")
    return None


def _resume_openclaw_telegram_polling(pids: list) -> None:
    """Resume OpenClaw gateway processes that were paused with SIGSTOP."""
    import signal

    for pid in pids:
        try:
            os.kill(pid, signal.SIGCONT)
            print(f"[2FA] Resumed OpenClaw gateway process (PID {pid})")
        except Exception as e:
            print(f"[2FA] WARNING: Could not resume PID {pid}: {e}")


def ask_telegram_for_2fa() -> Optional[str]:
    """
    Send a Telegram message asking the user for the 2FA code,
    then poll for their reply. Returns the 6-digit code or None.

    IMPORTANT: Pauses OpenClaw's gateway (SIGSTOP) during polling to prevent
    it from racing us for getUpdates messages. Resumes (SIGCONT) in finally.
    """
    import time

    token, chat_id = _get_telegram_creds()
    if not token or not chat_id:
        print("[TG] No Telegram credentials — cannot ask for 2FA code")
        return None

    # Pause OpenClaw's Telegram polling to prevent getUpdates race
    paused_pids = _stop_openclaw_telegram_polling()
    time.sleep(2)  # Let any in-flight getUpdates call complete

    try:
        # Get the latest update_id so we only look at NEW messages
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"limit": 1, "offset": -1, "timeout": 0},
                timeout=10
            )
            data = resp.json()
            last_update_id = 0
            if data.get("ok") and data.get("result"):
                last_update_id = data["result"][-1]["update_id"]
            print(f"[TG] Last update_id before asking: {last_update_id}")
        except Exception as e:
            print(f"[TG] Failed to get updates baseline: {e}")
            last_update_id = 0

        # Ask the user
        send_telegram(
            "I need your BCBS 2FA verification code. "
            "Check your email for the 6-digit code and reply here with it. "
            "I'll wait up to 5 minutes."
        )

        # Poll for reply (5 minutes, checking every 5 seconds)
        for attempt in range(60):
            time.sleep(5)
            try:
                resp = requests.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": last_update_id + 1, "timeout": 5},
                    timeout=15
                )
                data = resp.json()

                # Log first few attempts for debugging
                if attempt < 3:
                    print(f"[TG] Poll {attempt+1}: ok={data.get('ok')}, updates={len(data.get('result', []))}")

                if not data.get("ok"):
                    # 409 = conflict with another getUpdates consumer
                    if "409" in str(data):
                        print(f"[TG] WARNING: 409 Conflict — another process calling getUpdates!")
                    continue

                for update in data.get("result", []):
                    msg = update.get("message", {})
                    # Only accept messages from the right chat
                    if str(msg.get("chat", {}).get("id")) != str(chat_id):
                        last_update_id = update["update_id"]
                        continue
                    text = (msg.get("text") or "").strip()

                    # Log every message from the right chat
                    sender = msg.get("from", {})
                    print(f"[TG] Message from {sender.get('first_name', '?')}: '{text[:50]}'")

                    # Look for a 6-digit code in the reply
                    match = re.search(r'\b(\d{6})\b', text)
                    if match:
                        code = match.group(1)
                        print(f"[TG] Received 2FA code from user: ****{code[-2:]}")
                        return code
                    # Update offset to skip processed messages
                    last_update_id = update["update_id"]

            except Exception as e:
                print(f"[TG] Poll error: {e}")

            if attempt % 12 == 11:  # Every 60 seconds
                print(f"[TG] Still waiting for 2FA code... ({(attempt+1)*5}s elapsed)")

        print("[TG] Timed out waiting for 2FA code from user")
        send_telegram("Timed out waiting for 2FA code. Please try again.")
        return None

    finally:
        # ALWAYS resume gateway polling
        if paused_pids:
            _resume_openclaw_telegram_polling(paused_pids)


# ============================================================================
# MAIN CLAIM FILING FLOW
# ============================================================================

def file_single_claim(claim_data: dict) -> Tuple[str, str, Optional[str]]:
    """
    File a single claim via the API.

    Returns (status, message, ref), where status is the new column M value:
      "Filed"        — submitted, and the eClaim PDF shows the date + diagnosis
      "Needs Review" — submitted, but the eClaim PDF check failed (never refiled)
      "Failed"       — not submitted
      "Pending"      — not started; needs input (row left untouched, safe to rerun)
    """
    patient = claim_data["patient_name"]
    provider = claim_data["provider_name"]
    amount = claim_data["amount"]

    print(f"\n{'='*60}")
    print(f"[CLAIM] Filing claim for {patient}")
    print(f"[CLAIM] Provider: {provider}, Amount: {amount} {claim_data['currency']}")
    print(f"{'='*60}\n")

    try:
        # Resolve all reference data
        dep_id, sequence = resolve_patient(patient)
        country_id = resolve_country(claim_data["country"]) if claim_data["country"] else 24
        currency_id = resolve_currency(claim_data["currency"], country_id) if claim_data["currency"] else COUNTRY_CURRENCY.get(country_id, 220)
        # Validate the two inputs BCBS rejected claims over, before creating
        # anything at BCBS — so these rows stay Pending and are safe to rerun.
        try:
            date_api = format_date_api(claim_data["date_of_service"])
        except ValueError as e:
            return ("Pending", f"Not filed (row {claim_data['row_number']}): {e}. Fix column D and file again.", None)

        icd_code, diagnosis_desc = resolve_diagnosis(claim_data["diagnosis"], sequence)
        if icd_code == "ECLAIM":
            return ("Pending",
                    f"Not filed (row {claim_data['row_number']}): no diagnosis (column G is {claim_data['diagnosis']!r}). "
                    "BCBS rejects claims filed as OTHER. Put the CID from the doctor's "
                    "order in column G as \"code - description\", or tell me the reason "
                    "for the visit, then file again.", None)

        service_desc = resolve_service(
            claim_data["diagnosis"],
            procedure_codes=claim_data.get("procedure_codes", ""),
            bill_type=claim_data.get("bill_type", ""),
        )
        city = claim_data["city"].upper() if claim_data["city"] else ""

        print(f"[CLAIM] Resolved: dep_id={dep_id}, seq={sequence}, country={country_id}, "
              f"currency={currency_id}, icd={icd_code}, date={date_api}")

        # ── Step 1: Create claim + set claimant ──
        print("\n[STEP 1] Creating claim and setting claimant...")

        claimant = {
            "SubscriberID": None,
            "DependentID": dep_id,
            "Sequence": sequence,
            **DEFAULT_CLAIMANT
        }

        # Use patient-specific email for Fernanda
        if dep_id == 5000299527:
            claimant["EmailAddress"] = "fernanda.mdcruz@gmail.com"

        step1_body = {
            "Claim": make_claim_object(None),
            "ClaimantDetail": {
                "Claimant": claimant,
                "IsSportsInjury": False
            }
        }

        step1_resp = api_post("/claimants/save/", step1_body)
        claim_id = step1_resp.get("Claim", {}).get("ClaimSubmissionID")

        if not claim_id:
            return ("Failed", "Failed to create claim — no ClaimSubmissionID returned", None)

        print(f"[STEP 1] Claim created: ClaimSubmissionID={claim_id}")

        # ── Step 2: Set other insurance (none) ──
        print("\n[STEP 2] Setting other insurance (none)...")

        step2_body = {
            "Claim": make_claim_object(claim_id),
            "OtherInsuranceDetail": {
                "HasOtherInsurance": False,
                "OtherInsurance": {
                    "InsuranceID": None, "Address": None,
                    "CompanyName": None, "PolicyHolderFirstName": None,
                    "PolicyHolderMiddleName": None, "PolicyHolderLastName": None,
                    "PolicyHolderDateOfBirth": None, "PolicyIDNumber": None,
                    "EffectiveDate": None, "TerminationDate": None
                }
            }
        }

        api_post("/insurance/save/", step2_body)
        print("[STEP 2] Done")

        # ── Step 3: Add charge ──
        print("\n[STEP 3] Adding charge...")

        step3_body = {
            "Claim": make_claim_object(claim_id),
            "Charge": {
                "Documents": [],
                "ChargeID": None,
                "Name": f"CHG 1 {datetime.now().strftime('%d-%b-%Y').upper()}",
                "ProviderName": provider.upper(),
                "ProviderCity": city,
                "ProviderCountryID": country_id,
                "Diagnosis": diagnosis_desc,
                "ServiceDescription": service_desc,
                "ServiceStartDate": date_api,
                "ServiceEndDate": date_api,
                "Amount": str(amount),
                "CurrencyID": currency_id,
                "ProviderType": "Doctor",
                "ICD10Code": icd_code
            }
        }

        step3_resp = api_post("/charges/save/", step3_body)
        charge_id = step3_resp.get("Charge", {}).get("ChargeID")

        if not charge_id:
            return ("Failed", f"Failed to add charge — no ChargeID returned (claim {claim_id})", None)

        print(f"[STEP 3] Charge added: ChargeID={charge_id}")

        # ── Step 4: Upload supporting document (MANDATORY) ──
        if claim_data.get("drive_link"):
            print("\n[STEP 4] Uploading supporting document...")

            # Determine file extension from link or default to pdf
            link = claim_data["drive_link"]
            ext = "pdf"
            for e in ["jpg", "jpeg", "png", "pdf"]:
                if e in link.lower():
                    ext = e
                    break

            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp_path = tmp.name

            doc_uploaded = False
            try:
                if download_from_drive(link, tmp_path):
                    doc_info = upload_document(claim_id, charge_id, tmp_path, step3_body["Charge"])
                    if doc_info and doc_info.get("ChargeDocumentID"):
                        print(f"[STEP 4] Document uploaded: {doc_info.get('ChargeDocumentID')}")
                        doc_uploaded = True
                    else:
                        print("[STEP 4] FAILED: Document upload to BCBS failed")
                else:
                    print("[STEP 4] FAILED: Could not download file from Drive")
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            if not doc_uploaded:
                return ("Failed", f"Document upload failed for claim {claim_id} — claim NOT submitted (receipt is required). Drive link: {link}", None)
        else:
            # No drive link = no receipt = cannot submit
            return ("Failed", f"No supporting document link in sheet for claim {claim_id} — claim NOT submitted (receipt is required)", None)

        # ── Step 4b: Upload secondary document (if present) ──
        secondary_link = claim_data.get("secondary_doc", "").strip()
        if secondary_link:
            print("\n[STEP 4b] Uploading secondary supporting document...")

            sec_ext = "pdf"
            for e in ["jpg", "jpeg", "png", "pdf"]:
                if e in secondary_link.lower():
                    sec_ext = e
                    break

            with tempfile.NamedTemporaryFile(suffix=f".{sec_ext}", delete=False) as tmp:
                sec_tmp_path = tmp.name

            try:
                if download_from_drive(secondary_link, sec_tmp_path):
                    sec_doc_info = upload_document(claim_id, charge_id, sec_tmp_path, step3_body["Charge"])
                    if sec_doc_info and sec_doc_info.get("ChargeDocumentID"):
                        print(f"[STEP 4b] Secondary document uploaded: {sec_doc_info.get('ChargeDocumentID')}")
                    else:
                        print("[STEP 4b] WARNING: Secondary document upload to BCBS failed — continuing with primary doc only")
                else:
                    print(f"[STEP 4b] WARNING: Could not download secondary doc from Drive — continuing with primary doc only. Link: {secondary_link}")
            except Exception as e:
                print(f"[STEP 4b] WARNING: Secondary document upload error: {e} — continuing with primary doc only")
            finally:
                if os.path.exists(sec_tmp_path):
                    os.unlink(sec_tmp_path)

        # ── Step 4c: Verify BCBS stored the date + diagnosis (BEFORE submitting) ──
        # The API returns 200 for data it then drops, so check what it kept.
        # Stopping here leaves an unsubmitted draft — nothing reaches BCBS.
        print("\n[STEP 4c] Verifying saved charge before submitting...")
        problems = verify_saved_charge(claim_id, charge_id, date_api, icd_code)
        if problems:
            return ("Failed",
                    f"NOT submitted — BCBS did not keep the charge as sent: {'; '.join(problems)}. "
                    f"Draft claim {claim_id} left unsubmitted.", None)
        print("[STEP 4c] Service dates and diagnosis confirmed on the saved charge")

        # ── Step 5: Set payment account ──
        print("\n[STEP 5] Setting payment account...")

        step5_body = {
            "Claim": make_claim_object(claim_id),
            "PaymentAccountDetail": {
                "PaymentMethod": "WIRE",
                "PaymentAccount": SAVED_PAYMENT_ACCOUNT
            }
        }

        api_post("/paymentaccounts/save/", step5_body)
        print("[STEP 5] Payment account set")

        # ── Step 6: Submit claim ──
        print("\n[STEP 6] Submitting claim...")

        # Determine signature based on patient
        if dep_id == 5000299527:
            signature = "Fernanda Miranda da Cruz"
        elif dep_id is None:
            signature = "Max Jacobson"
        else:
            # For children, use parent signature
            signature = "Fernanda Miranda da Cruz"

        step6_body = {
            "Claim": {
                **make_claim_object(claim_id),
                "HasAgreedToTerms": True,
                "Signature": signature
            },
            "SupportingDocument": {}
        }

        step6_resp = api_post("/claims/submit", step6_body)

        submitted_claim = step6_resp.get("Claim", {})
        submitted_date = submitted_claim.get("SubmittedDate")

        if not (submitted_date or submitted_claim.get("ClaimSubmissionID")):
            return ("Failed", "Claim submission may have failed — no confirmation in response", None)

        ref = f"CLM-{claim_id}"
        print(f"\n[SUBMITTED] ID={claim_id}, Date={submitted_date}")

        # ── Step 7: Check the eClaim PDF BCBS generated ──
        # Submitted is not the same as Filed: only mark Filed once the form
        # BCBS will process shows our service date and a real diagnosis.
        print("\n[STEP 7] Checking the submitted eClaim PDF...")
        problems = verify_eclaim_pdf(claim_id, date_api, icd_code, diagnosis_desc)
        if problems:
            return ("Needs Review",
                    f"Submitted as {ref}, but the eClaim PDF check failed: {'; '.join(problems)}. "
                    "Check it in the BCBS portal — NOT marked Filed.", ref)

        print(f"[SUCCESS] eClaim PDF shows the service date and diagnosis")
        return ("Filed", f"Claim filed and verified. Reference: {ref} (ID: {claim_id})", ref)

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\n[ERROR] Claim filing failed: {e}\n{tb}")
        return ("Failed", f"Error: {str(e)}", None)


def authenticate() -> bool:
    """
    Obtain an OAuth token via Playwright login and set it on the API session.
    Returns True if token was obtained, False otherwise.
    """
    import asyncio

    print("[AUTH] API requires authentication — obtaining OAuth token via Playwright login...")
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If we're already in an async context, create a new loop in a thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                token = pool.submit(lambda: asyncio.run(obtain_oauth_token())).result(timeout=120)
        else:
            token = loop.run_until_complete(obtain_oauth_token())
    except RuntimeError:
        token = asyncio.run(obtain_oauth_token())

    if token:
        set_auth_token(token)
        print("[AUTH] Token set — API calls will now include Authorization header")
        return True
    else:
        print("[AUTH] Failed to obtain token")
        return False


def test_api_auth() -> bool:
    """
    Quick test: try a lightweight API call to see if auth is needed.
    Returns True if API works (with or without auth), False if auth is needed but missing.
    """
    try:
        resp = session.get(f"{API_BASE}/claims/metadata/", timeout=10)
        if resp.status_code == 200:
            print("[AUTH] API accessible without additional auth")
            return True
        elif resp.status_code in (401, 403):
            print(f"[AUTH] API returned {resp.status_code} — authentication required")
            return False
        else:
            print(f"[AUTH] API returned unexpected status {resp.status_code}")
            return False
    except Exception as e:
        print(f"[AUTH] API test failed: {e}")
        return False


def main():
    """Main entry point: read pending claims from Google Sheets and file them."""
    print(f"\n[MAIN] BCBS API Claim Filer {SCRIPT_VERSION}")
    print(f"[MAIN] Time: {datetime.now().isoformat()}")

    if not GOOGLE_SHEET_ID:
        print("[MAIN] ERROR: GOOGLE_SHEET_ID not set")
        send_telegram("Claim filing failed: GOOGLE_SHEET_ID not configured")
        return

    # ── Step 0: Check if API needs auth, and if so, login to get token ──
    # Shortcut: if BCBS_TOKEN is set, use it directly (skip Playwright login entirely)
    manual_token = os.environ.get("BCBS_TOKEN")
    if manual_token:
        print(f"[AUTH] Using manually provided BCBS_TOKEN (length: {len(manual_token)})")
        set_auth_token(manual_token)
    elif not test_api_auth():
        if not authenticate():
            msg = "Claim filing failed: could not obtain BCBS OAuth token. Check BCBS_USERNAME/BCBS_PASSWORD env vars, or set BCBS_TOKEN manually."
            print(f"[MAIN] {msg}")
            send_telegram(msg)
            return

    # Read pending claims
    claims = read_pending_claims()

    if not claims:
        print("[MAIN] No pending claims found")
        send_telegram("No pending claims to file.")
        return

    # File each claim
    results = []
    for claim in claims:
        status, message, ref = file_single_claim(claim)
        results.append((claim, status, message))
        if status != "Pending":  # Pending = not started; leave the row as-is
            update_sheets(claim["row_number"], ref or "", status)

    # Build summary
    counts = {s: sum(1 for _, st, _ in results if st == s)
              for s in ("Filed", "Needs Review", "Failed", "Pending")}
    summary_lines = ["Claim filing complete: " + ", ".join(
        f"{n} {s.lower()}" for s, n in counts.items() if n)]
    tags = {"Filed": "OK", "Needs Review": "CHECK", "Failed": "FAIL", "Pending": "NEEDS INFO"}
    for claim, status, message in results:
        summary_lines.append(f"  [{tags[status]}] {claim['patient_name']} / {claim['provider_name']}: {message}")

    summary = "\n".join(summary_lines)
    print(f"\n[SUMMARY]\n{summary}")
    send_telegram(summary)


if __name__ == "__main__":
    main()
