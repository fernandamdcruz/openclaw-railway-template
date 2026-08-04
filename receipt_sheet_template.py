exec: python3 << 'SHEETEOF'
import os, sys, json
from datetime import datetime, timezone

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError as e:
    sys.exit(f"ERROR: Google API libraries are missing ({e}). These belong in the "
             "Dockerfile (google-api-python-client, google-auth) — do NOT pip "
             "install at runtime. Report this; it needs an image rebuild.")

sa_key = os.environ.get('GOOGLE_SA_KEY')
if not sa_key:
    sys.exit("ERROR: GOOGLE_SA_KEY env var is not set. It must be configured in "
             "Railway Variables — the row cannot be appended without it.")
try:
    creds_info = json.loads(sa_key)
except json.JSONDecodeError as e:
    sys.exit(f"ERROR: GOOGLE_SA_KEY is not valid JSON ({e}). Re-paste the service "
             "account key into Railway Variables.")

creds = service_account.Credentials.from_service_account_info(
    creds_info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
sheets = build('sheets', 'v4', credentials=creds)

# One tab per year — derive so this keeps working after Jan 1 with no code change
TAB = os.environ.get('GOOGLE_SHEET_TAB', str(datetime.now(timezone.utc).year))

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
    range=f'{TAB}!A:R',
    valueInputOption='USER_ENTERED',
    insertDataOption='INSERT_ROWS',
    body={'values': row}).execute()
print(f"OK: {len(row[0])} columns written as one row to tab '{TAB}'")
SHEETEOF
