#!/usr/bin/env python3
"""
Quick test: file one specific claim using a manually provided BCBS token.
Usage: BCBS_TOKEN=<token> python3 test_file_claim.py

This bypasses gog/sheets/Playwright — just tests the API filing flow directly.
"""
import os
import sys
import json
import requests
from datetime import datetime

TOKEN = os.environ.get("BCBS_TOKEN", "")
if not TOKEN:
    print("Set BCBS_TOKEN env var first")
    sys.exit(1)

API_BASE = "https://claimsapire.hthworldwide.com/v4"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://members.bcbsglobalsolutions.com",
    "Referer": "https://members.bcbsglobalsolutions.com/",
    "Authorization": f"Bearer {TOKEN}",
}

session = requests.Session()
session.headers.update(HEADERS)

USER_ID = 240216564258281
PEOPLE_ID = "502968557"

def make_claim_object(claim_submission_id=None):
    """Full Claim object matching claim_filer_api.py's make_claim_object."""
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
        "PaymentMethod": "WIRE",
    }

# Row 19 claim data
CLAIM = {
    "patient_name": "Fernanda Dutra de Oliveira Miranda da Cruz",
    "provider_name": "Clínica Oftalmológica SW Ltda",
    "date_of_service": "2026-02-27",
    "amount": "1150",
    "currency": "BRL",
    "diagnosis": "",
    "procedure_codes": "4030",
    "invoice_number": "5285",
    "city": "Sao Paulo",
    "country": "Brazil",
    "notes": "Medical consultation - Dra. Christiane S. Wakisaka CRM-SP 75580",
}

def api_post(endpoint, body):
    url = f"{API_BASE}{endpoint}"
    print(f"\n>>> POST {url}")
    print(f"    Body: {json.dumps(body)[:300]}")
    resp = session.post(url, json=body, timeout=30)
    print(f"    Status: {resp.status_code}")
    try:
        data = resp.json()
        print(f"    Response: {json.dumps(data)[:300]}")
        return data
    except:
        print(f"    Response (text): {resp.text[:300]}")
        return {}

# Step 1: Create claim (claimants/save)
print("=" * 60)
print("STEP 1: Create claim (claimants/save)")
print("=" * 60)

claimant_body = {
    "Claim": make_claim_object(None),
    "ClaimantDetail": {
        "Claimant": {
            "SubscriberID": None,
            "DependentID": 5000299527,
            "Sequence": "03",
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
        },
        "IsSportsInjury": False,
    }
}

result = api_post("/claimants/save/", claimant_body)
claim_id = result.get("Claim", {}).get("ClaimSubmissionID")
if not claim_id:
    print(f"\nFAILED: No ClaimSubmissionID returned")
    sys.exit(1)
print(f"\n*** Claim created: ID = {claim_id}")

# Step 2: Insurance (none)
print("\n" + "=" * 60)
print("STEP 2: Other insurance (insurance/save)")
print("=" * 60)

insurance_body = {
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
api_post("/insurance/save/", insurance_body)

# Step 3: Add charge
print("\n" + "=" * 60)
print("STEP 3: Add charge (charges/save)")
print("=" * 60)

charge_body = {
    "Claim": make_claim_object(claim_id),
    "Charge": {
        "Documents": [],
        "ChargeID": None,
        "Name": f"CHG 1 {datetime.now().strftime('%d-%b-%Y').upper()}",
        "ProviderName": CLAIM["provider_name"].upper(),
        "ProviderCity": CLAIM["city"],
        "ProviderCountryID": 24,    # Brazil
        "Diagnosis": "Medical consultation - Ophthalmology",
        "ServiceDescription": CLAIM["procedure_codes"],
        "ServiceStartDate": "20260227",  # YYYYMMDD format required
        "ServiceEndDate": "20260227",
        "Amount": CLAIM["amount"],
        "CurrencyID": 220,  # BRL
        "ProviderType": "Doctor",
        "ICD10Code": "H52.10",
    }
}
charge_result = api_post("/charges/save/", charge_body)
charge_id = charge_result.get("Charge", {}).get("ChargeID")
print(f"\n*** Charge created: ID = {charge_id}")

# Step 4: Skip document upload for now (just test the flow)
print("\n" + "=" * 60)
print("STEP 4: Skipping document upload for this test")
print("=" * 60)

# Step 5: Payment
print("\n" + "=" * 60)
print("STEP 5: Payment (paymentaccounts/save)")
print("=" * 60)

payment_body = {
    "Claim": make_claim_object(claim_id),
    "PaymentAccountDetail": {
        "PaymentMethod": "WIRE",
        "PaymentAccount": {
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
    }
}
api_post("/paymentaccounts/save/", payment_body)

# Step 6: Submit
print("\n" + "=" * 60)
print("STEP 6: Submit claim (claims/submit)")
print("=" * 60)

submit_body = {
    "Claim": make_claim_object(claim_id),
    "Signature": "Fernanda Miranda da Cruz",
    "HasAgreedToTerms": True,
}

# DON'T actually submit yet — just show what would happen
print("\n*** NOT SUBMITTING — this is a dry run ***")
print(f"    Would POST to /claims/submit with ClaimSubmissionID={claim_id}")
print(f"    Submit body: {json.dumps(submit_body, indent=2)}")
print(f"\n*** To actually submit, uncomment the api_post line below and run again ***")
# api_post("/claims/submit", submit_body)

print("\n" + "=" * 60)
print("TEST COMPLETE")
print(f"Claim ID: {claim_id}")
print(f"Charge ID: {charge_id}")
print("Steps 1-3 and 5 executed. Step 4 (doc upload) and Step 6 (submit) skipped.")
print("=" * 60)
