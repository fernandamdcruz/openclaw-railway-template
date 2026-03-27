# File BCBS Claim — Skill Definition

## Trigger
"file claim", "file my claims", "submit reimbursement", "BCBS claim", or similar.

## What To Do

Run the **API-based** claim filer script. It files claims via direct HTTP requests to the BCBS/GeoBlue API — no browser, no login, no 2FA needed.

```bash
python3 /data/workspace/skills/file-claim/claim_filer_api.py
```

If the API script is not available or fails with a connection error, fall back to the Playwright script:

```bash
python3 /data/workspace/skills/file-claim/claim_filer.py
```

**CRITICAL: As soon as the script finishes (whether it succeeded or failed), IMMEDIATELY message Fernanda with the result.** Do not wait. Do not do anything else first. She is waiting for your reply. Look for the `[SUMMARY]` or `[RESULT]` line at the end of the script output and send that to her, plus a brief summary of what happened.

## If The Script Fails

1. **IMMEDIATELY message Fernanda** with the error output.
2. **STOP.** Do NOT try to fix the script, edit the code, or fall back to manual browser automation.
3. Wait for Fernanda's instructions.

> **Manual fallback exists** in `MANUAL_FALLBACK.md` (same directory), but **only read it if Fernanda explicitly tells you to continue manually.** Never read it on your own initiative.

## How The API Script Works

The script files claims in 6 API calls (no browser needed):
1. `POST /v4/claimants/save/` — Create claim + set patient
2. `POST /v4/insurance/save/` — Set other insurance (none)
3. `POST /v4/charges/save/` — Add charge (provider, diagnosis, amount, dates)
4. Upload supporting document to S3 via presigned URL
5. `POST /v4/paymentaccounts/save/` — Set saved wire payment account
6. `POST /v4/claims/submit` — Submit with signature

Diagnosis and Service Description are resolved dynamically by fetching the available dropdown options from the API and fuzzy-matching against the spreadsheet data.

## Provider → Patient Mapping

- CLINICA LIVIDI / Clínica Lividi Med → patient: **Elena Miranda** (never Mathias)
- Dr. Rohrmoser → can be either **Fernanda** or **Mathias** — always check the Patient Name in column B of the sheet

## Data Reference

### Env vars (set in Railway)
- `BCBS_USERNAME` / `BCBS_PASSWORD` — portal credentials (only needed for Playwright fallback)
- Config home env var set to `/data/workspace/.config` — gog Gmail/Sheets access
- `GOG_KEYRING_PASSWORD=ferdybot-calendar-2026` — gog keyring

### Google Sheets
- Sheet ID: `1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk`
- Tab: `2026`

### Telegram
- Chat ID: `8409634074`
