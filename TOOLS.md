# TOOLS.md - Local Notes

Skills define how tools work. This file is for your specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:
* Camera names and locations
* SSH hosts and aliases
* Preferred voices for TTS
* Speaker/room names
* Device nicknames
* Anything environment-specific

**Why Separate?** Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

## Google Calendar (gog CLI)

**Account:** fernanda.mdcruz@gmail.com
**Location:** /data/workspace/.config/gogcli/

**Usage:**
```bash
# Source the config before running gog commands
source /data/workspace/.gogrc
# Or run directly with environment variables
XDG_CONFIG_HOME=/data/workspace/.config GOG_KEYRING_PASSWORD="$GOG_KEYRING_PASSWORD" gog calendar list --account fernanda.mdcruz@gmail.com --today
```

**Quick commands:**
* List today: `gog calendar list --account fernanda.mdcruz@gmail.com --today`
* List tomorrow: `gog calendar list --account fernanda.mdcruz@gmail.com --tomorrow`
* List this week: `gog calendar list --account fernanda.mdcruz@gmail.com --week`

**Credentials:**
* OAuth credentials: /data/workspace/.config/gogcli/credentials.json
* Keyring (encrypted tokens): /data/workspace/.config/gogcli/keyring/
* Config: /data/workspace/.config/gogcli/config.json

---

## MEDICAL RECEIPT PROCESSING

**Workflow:** When Fernanda sends a medical/dental bill image or PDF via Telegram, process automatically without asking for confirmation.

**Environment Variables:**
```
XDG_CONFIG_HOME=/data/workspace/.config
GOG_KEYRING_PASSWORD="$GOG_KEYRING_PASSWORD"
```

### STEP 1 - READ THE RECEIPT

Use vision to extract: Patient Name, Provider Name, Date of Service (YYYY-MM-DD), Amount Billed, Currency (default BRL), Diagnosis Codes, Procedure Code, Invoice #, Year, City (where treatment occurred), Country (where treatment occurred), Notes, Bill Type: "Dental" or "Medical"

### STEP 2 - UPLOAD TO GOOGLE DRIVE

Save each image/PDF to /tmp/ first, then upload to Drive.

**Primary bill** (nota fiscal / main receipt) - save to /tmp/bill_file:
```
exec: XDG_CONFIG_HOME=/data/workspace/.config GOG_KEYRING_PASSWORD="$GOG_KEYRING_PASSWORD" gog drive upload /tmp/bill_file --account fernanda.mdcruz@gmail.com --parent 1Mm3GMzLGngijoEbTHYuCPVeAUMZaN5Mh 2>&1
```

Parse output to extract the `link` field -> this is PRIMARY_DRIVE_LINK (used as DRIVE_LINK in STEP 3).

**Secondary document** (pedido medico, referral, or diagnosis) - ONLY if a second image was sent:
If Fernanda sent two images in the same message, save the second one to /tmp/bill_file2 and upload:
```
exec: XDG_CONFIG_HOME=/data/workspace/.config GOG_KEYRING_PASSWORD="$GOG_KEYRING_PASSWORD" gog drive upload /tmp/bill_file2 --account fernanda.mdcruz@gmail.com --parent 1Mm3GMzLGngijoEbTHYuCPVeAUMZaN5Mh 2>&1
```

If only one image was sent, SECONDARY_DRIVE_LINK = '' (empty string).

### STEP 3 - APPEND ROW TO GOOGLE SHEETS

Run this Python script, replacing each PLACEHOLDER with the real extracted value.

**Column Mapping (A through R, 18 columns total):**
* A = Date Processed (UTC timestamp)
* B = Patient Name
* C = Provider Name
* D = Date of Service (YYYY-MM-DD)
* E = Amount Billed
* F = Currency
* G = Diagnosis Codes
* H = Procedure Codes
* I = Invoice Number
* J = Year
* K = City (where treatment occurred) [REQUIRED]
* L = Country (where treatment occurred) [REQUIRED]
* M = Claim Status (default: "Pending")
* N = Drive Link (primary document)
* O = Bill Type ("Medical" or "Dental")
* P = Secondary Drive Link
* Q = Claim Ref # (filled by filing script)
* R = Notes

**CRITICAL RULES:**
* Use triple-doublequotes around EVERY placeholder value
* The clean() function collapses multiline text to one line - always use it
* ONE single sheets.append() call - NEVER split data across multiple calls
* If the script fails, tell Fernanda the error - do NOT attempt partial appends
* ALWAYS write 18 columns (A through R), not 16

```python
exec: python3 << 'SHEETEOF'
import sys, os, json
sys.path.insert(0, os.path.expanduser('~/.local/lib/python3.11/site-packages'))
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timezone

creds_info = json.loads(os.environ['GOOGLE_SA_KEY'])
creds = service_account.Credentials.from_service_account_info(
    creds_info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
sheets = build('sheets', 'v4', credentials=creds)

def clean(s):
    return ' '.join(str(s).split())

v1 = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
v2 = clean("""PATIENT_NAME""")
v3 = clean("""PROVIDER_NAME""")
v4 = clean("""DATE_OF_SERVICE""")
v5 = clean("""AMOUNT""")
v6 = clean("""CURRENCY""")
v7 = clean("""DIAGNOSIS_CODES""")
v8 = clean("""PROCEDURE_CODE""")
v9 = clean("""INVOICE_NUMBER""")
v10 = clean("""YEAR""")
v11 = clean("""CITY""")
v12 = clean("""COUNTRY""")
v13 = 'Pending'
v14 = clean("""DRIVE_LINK""")
v15 = clean("""BILL_TYPE""")
v16 = clean("""SECONDARY_DRIVE_LINK""")
v17 = ''
v18 = clean("""NOTES""")

row = [[v1, v2, v3, v4, v5, v6, v7, v8, v9, v10, v11, v12, v13, v14, v15, v16, v17, v18]]
assert len(row[0]) == 18, f"BUG: {len(row[0])} cols."
sheets.spreadsheets().values().append(
    spreadsheetId='1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk',
    range='2026!A:R',
    valueInputOption='RAW',
    insertDataOption='INSERT_ROWS',
    body={'values': row}).execute()
print(f"OK: {len(row[0])} columns written as one row")
SHEETEOF
```

### STEP 4 - REPLY TO FERNANDA

Send Telegram: "Receipt processed: [Provider] - [Amount] [Currency] on [Date]. Bill Type: [Medical/Dental]. Saved to Drive and Medical Bills sheet."

### STEP 5 - FILE GEOBLUE CLAIM

Use the `file-claim` skill. Trigger phrases: "file", "file claim", "submit claim", "file reimbursement", "submit reimbursement"

---

*Add whatever helps you do your job. This is your cheat sheet.*
