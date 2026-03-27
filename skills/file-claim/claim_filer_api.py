#!/usr/bin/env python3
"""
BCBS Global Solutions — Direct API Claim Filer
================================================
Files medical claims via the BCBS/GeoBlue REST API (claimsapire.hthworldwide.com).

NO browser automation, NO Playwright, NO Okta login, NO 2FA.
The claims API has no authentication — it uses UserID in the request body.

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
  GOOGLE_SHEET_TAB      — Tab name (default: "Medical Bills")
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

# Try to import requests, install if missing
try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--break-system-packages", "-q"])
    import requests

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_VERSION = "api-v7-tg-from-config-2026-03-27"
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

# Google Sheets config
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Medical Bills")

# gog CLI environment
GOG_ENV = os.environ.copy()
gog_config = os.environ.get("GOG_CONFIG_DIR")
if gog_config:
    GOG_ENV["GOG_CONFIG_DIR"] = gog_config


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

    # Import 2FA helper from the Playwright script (same directory)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)
    try:
        from claim_filer import get_2fa_code_from_gmail
    except ImportError:
        print("[AUTH] Could not import get_2fa_code_from_gmail — 2FA will fail")
        get_2fa_code_from_gmail = None

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

            # Click Login button (Flutter landing page)
            login_btn = page.get_by_role("button", name=re.compile("^login$", re.IGNORECASE))
            if await login_btn.count() > 0:
                await login_btn.click()
                await asyncio.sleep(5)
                print(f"[AUTH] Redirected to: {page.url}")

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

            # Submit login
            import time as _time
            login_epoch = int(_time.time())
            submit_btn = page.locator('input[type="submit"][value="SIGN IN"]')
            await submit_btn.click()
            print(f"[AUTH] SIGN IN clicked (epoch: {login_epoch})")
            await asyncio.sleep(5)

            # Check for 2FA
            current_url = page.url
            page_text = await page.text_content("body") or ""

            if "verification" in page_text.lower() or "code" in page_text.lower() or "factor" in page_text.lower():
                print("[AUTH] 2FA detected — asking user for code via Telegram")

                # Ask the user directly via Telegram (most reliable)
                code = ask_telegram_for_2fa()

                # Fallback: try Gmail auto-extraction if Telegram ask failed
                if not code and get_2fa_code_from_gmail:
                    print("[AUTH] No code from Telegram, trying Gmail auto-extraction")
                    code = get_2fa_code_from_gmail(login_epoch)

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


def resolve_diagnosis(diagnosis_text: str, sequence: str = "03") -> Tuple[str, str]:
    """
    Resolve free-text diagnosis to (ICD10Code, Description) accepted by the API.

    Strategy:
    1. Fetch available diagnoses from API (patient-specific + generic)
    2. Try exact ICD-10 code match (e.g. "L70.0" → L700)
    3. Try fuzzy text match against available descriptions
    4. Fall back to keyword map
    5. Default to OTHER
    """
    text = diagnosis_text.strip()
    if not text:
        return ("ECLAIM", "OTHER")

    print(f"[DIAG] Resolving diagnosis: '{text}'")

    # Fetch available options from API
    options = fetch_diagnosis_options(sequence)

    if options:
        # ── Try 1: Exact ICD-10 code match ──
        # Strip dots/spaces from input: "L70.0" → "L700", "R21" → "R21"
        input_code = re.sub(r'[.\s-]', '', text).upper()
        for opt in options:
            opt_code = re.sub(r'[.\s-]', '', opt["Icd10"]).upper()
            if input_code == opt_code:
                print(f"[DIAG] Exact ICD-10 match: {opt['Icd10']} = {opt['Description']}")
                return (opt["Icd10"], opt["Description"])

        # ── Try 2: Fuzzy match against both code AND description ──
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

    # ── Try 3: Keyword fallback ──
    key = text.lower()
    for keyword, (icd, desc) in DIAGNOSIS_KEYWORD_FALLBACK.items():
        if keyword in key:
            print(f"[DIAG] Keyword fallback '{keyword}': {icd} = {desc}")
            return (icd, desc)

    # ── Try 4: If input looks like an ICD-10 code, use OTHER with a note ──
    if re.match(r'^[A-Z]\d', text.upper()):
        print(f"[DIAG] Input looks like ICD-10 code '{text}' but no match found, using OTHER")
        return ("ECLAIM", "OTHER")

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
    """Convert various date formats to YYYYMMDD for the API."""
    # Try common formats
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y",
                "%Y%m%d", "%m-%d-%Y", "%d.%m.%Y"]:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y%m%d")
        except ValueError:
            continue

    # Last resort: try to extract numbers
    nums = re.findall(r'\d+', date_str)
    if len(nums) >= 3:
        # Assume YYYY-MM-DD or similar
        if len(nums[0]) == 4:
            return f"{nums[0]}{nums[1]:0>2}{nums[2]:0>2}"
        elif len(nums[2]) == 4:
            return f"{nums[2]}{nums[0]:0>2}{nums[1]:0>2}"

    raise ValueError(f"Cannot parse date: {date_str}")


# ============================================================================
# DOCUMENT UPLOAD
# ============================================================================

def download_from_drive(drive_link: str, output_path: str) -> bool:
    """Download a file from Google Drive using gog CLI."""
    print(f"[DOC] Downloading from Drive: {drive_link}")

    # Extract file ID from various Drive URL formats
    file_id = None
    if "/d/" in drive_link:
        file_id = drive_link.split("/d/")[1].split("/")[0].split("?")[0]
    elif "id=" in drive_link:
        file_id = drive_link.split("id=")[1].split("&")[0]
    elif drive_link.startswith("http"):
        # Try the whole URL
        file_id = drive_link
    else:
        file_id = drive_link

    if not file_id:
        print(f"[DOC] Could not extract file ID from: {drive_link}")
        return False

    try:
        result = subprocess.run(
            ["gog", "drive", "download", file_id, "--output", output_path],
            capture_output=True, text=True, timeout=60, env=GOG_ENV
        )
        if result.returncode == 0:
            file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            print(f"[DOC] Downloaded successfully: {file_size} bytes")
            return file_size > 0
        else:
            print(f"[DOC] Download failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        print(f"[DOC] Download error: {e}")
        return False


def upload_document(claim_id: int, charge_id: int, file_path: str) -> Optional[dict]:
    """
    Upload a supporting document to the claim.
    1. POST /chargedocuments/Initiate → get presigned S3 URL
    2. PUT to S3 → upload file
    3. POST /chargedocuments/Complete → confirm
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

    # Step 3: Confirm upload
    complete_body = {
        "Claim": make_claim_object(claim_id),
        "Charge": {
            "Documents": [],
            "ChargeID": charge_id,
        },
        "ChargeDocument": {
            "Name": filename,
            "FileExtension": extension,
            "FileETag": etag,
            "FilePath": s3_path
        }
    }

    # We need the full charge data for the Complete call
    # Fetch it from charges/forclaim
    charges = api_get(f"/charges/forclaim/{claim_id}/")
    if charges and isinstance(charges, list):
        for c in charges:
            if c.get("ChargeID") == charge_id:
                complete_body["Charge"] = c
                complete_body["Charge"]["Documents"] = []  # Reset docs for this call
                break

    complete_resp = api_post("/chargedocuments/Complete", complete_body)

    doc_info = complete_resp.get("ChargeDocument", {})
    print(f"[DOC] Upload confirmed: ChargeDocumentID={doc_info.get('ChargeDocumentID')}")
    return doc_info


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


def ask_telegram_for_2fa() -> Optional[str]:
    """
    Send a Telegram message asking the user for the 2FA code,
    then poll for their reply. Returns the 6-digit code or None.
    """
    import time

    token, chat_id = _get_telegram_creds()
    if not token or not chat_id:
        print("[TG] No Telegram credentials — cannot ask for 2FA code")
        return None

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
            if not data.get("ok"):
                continue

            for update in data.get("result", []):
                msg = update.get("message", {})
                # Only accept messages from the right chat
                if str(msg.get("chat", {}).get("id")) != str(chat_id):
                    continue
                text = (msg.get("text") or "").strip()
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


# ============================================================================
# MAIN CLAIM FILING FLOW
# ============================================================================

def file_single_claim(claim_data: dict) -> Tuple[bool, str]:
    """
    File a single claim via the API.
    Returns (success: bool, message: str).
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
        icd_code, diagnosis_desc = resolve_diagnosis(claim_data["diagnosis"], sequence)
        service_desc = resolve_service(
            claim_data["diagnosis"],
            procedure_codes=claim_data.get("procedure_codes", ""),
            bill_type=claim_data.get("bill_type", ""),
        )
        date_api = format_date_api(claim_data["date_of_service"])
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
            return (False, "Failed to create claim — no ClaimSubmissionID returned")

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
            return (False, f"Failed to add charge — no ChargeID returned (claim {claim_id})")

        print(f"[STEP 3] Charge added: ChargeID={charge_id}")

        # ── Step 4: Upload supporting document ──
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

            try:
                if download_from_drive(link, tmp_path):
                    doc_info = upload_document(claim_id, charge_id, tmp_path)
                    if doc_info:
                        print(f"[STEP 4] Document uploaded: {doc_info.get('ChargeDocumentID')}")
                    else:
                        print("[STEP 4] WARNING: Document upload failed, continuing without doc")
                else:
                    print("[STEP 4] WARNING: Could not download from Drive, continuing without doc")
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        else:
            print("\n[STEP 4] No supporting document link, skipping upload")

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

        if submitted_date:
            ref = f"CLM-{claim_id}"
            print(f"\n[SUCCESS] Claim submitted! ID={claim_id}, Date={submitted_date}")
            return (True, f"Claim filed successfully! Reference: {ref} (ID: {claim_id}), Submitted: {submitted_date}")
        else:
            # Check if submission ID exists at least
            if submitted_claim.get("ClaimSubmissionID"):
                ref = f"CLM-{claim_id}"
                print(f"\n[SUCCESS] Claim submitted (no date in response). ID={claim_id}")
                return (True, f"Claim filed! Reference: {ref} (ID: {claim_id})")
            else:
                return (False, f"Claim submission may have failed — no confirmation in response")

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\n[ERROR] Claim filing failed: {e}\n{tb}")
        return (False, f"Error: {str(e)}")


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
        success, message = file_single_claim(claim)
        results.append((claim, success, message))

        if success:
            # Extract claim ID from message
            ref_match = re.search(r'ID:\s*(\d+)', message)
            ref = ref_match.group(1) if ref_match else "FILED"
            update_sheets(claim["row_number"], f"CLM-{ref}", "Filed")
        else:
            update_sheets(claim["row_number"], "", "Failed")

    # Build summary
    filed = sum(1 for _, s, _ in results if s)
    failed = sum(1 for _, s, _ in results if not s)

    summary_lines = [f"Claim filing complete: {filed} filed, {failed} failed"]
    for claim, success, message in results:
        emoji = "OK" if success else "FAIL"
        summary_lines.append(f"  [{emoji}] {claim['patient_name']} / {claim['provider_name']}: {message}")

    summary = "\n".join(summary_lines)
    print(f"\n[SUMMARY]\n{summary}")
    send_telegram(summary)


if __name__ == "__main__":
    main()
