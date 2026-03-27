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

SCRIPT_VERSION = "api-v1-2026-03-27"
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
# API CLIENT
# ============================================================================

session = requests.Session()
session.headers.update(API_HEADERS)


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

def send_telegram(message: str) -> None:
    """Send a message to Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
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


def main():
    """Main entry point: read pending claims from Google Sheets and file them."""
    print(f"\n[MAIN] BCBS API Claim Filer {SCRIPT_VERSION}")
    print(f"[MAIN] Time: {datetime.now().isoformat()}")

    if not GOOGLE_SHEET_ID:
        print("[MAIN] ERROR: GOOGLE_SHEET_ID not set")
        send_telegram("Claim filing failed: GOOGLE_SHEET_ID not configured")
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
