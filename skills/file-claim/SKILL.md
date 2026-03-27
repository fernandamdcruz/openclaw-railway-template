# File BCBS Claim — Skill Definition

## Trigger
"file claim", "file my claims", "submit reimbursement", "BCBS claim", or similar.

## What To Do

Run the claim filer script. **Run it exactly as shown — do not modify, do not improvise, do not read the source code to "understand" it.**

```bash
GOOGLE_SHEET_ID=1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk GOOGLE_SHEET_TAB=2026 python3 /data/workspace/skills/file-claim/claim_filer_api.py
```

The script handles EVERYTHING internally:
- Reads pending claims from Google Sheets
- Logs into BCBS via Playwright if needed (handles OAuth + 2FA)
- **It will message Fernanda on Telegram asking for her 2FA code — wait for the script to handle this, do NOT ask her yourself**
- Files each claim via API calls
- Updates the spreadsheet
- Sends Telegram notifications

**YOUR ONLY JOB is to run the command above and relay the output to Fernanda.**

## CRITICAL RULES

1. **DO NOT** ask Fernanda for her BCBS password or any credentials. The script reads them from environment variables.
2. **DO NOT** open a browser yourself. The script manages its own Playwright browser.
3. **DO NOT** read or modify the Python source code.
4. **DO NOT** try to "help" the script by running parts of it manually.
5. **DO NOT** fall back to manual browser automation if the script fails.
6. **DO NOT** investigate, analyze, or debug anything yourself. Just run the command and report the output.

## COST WARNING

Every token you use costs Fernanda real money. Do NOT ramble, do NOT investigate, do NOT read source files to "understand" them. Run the one command, report the result, stop. Long exploratory sessions where you read files, analyze code, and try multiple approaches are EXTREMELY expensive and waste her money. Be brief. Be direct. Run the script. Report. Stop.

## If The Script Fails

1. **IMMEDIATELY message Fernanda** with the FULL error output.
2. **STOP.** Do NOT try to fix anything.
3. Wait for Fernanda's instructions.

## Provider → Patient Mapping

- CLINICA LIVIDI / Clínica Lividi Med → patient: **Elena Miranda** (never Mathias)
- Dr. Rohrmoser → can be either **Fernanda** or **Mathias** — always check the Patient Name in column B of the sheet

## Env vars (set in Railway)
- `BCBS_USERNAME` / `BCBS_PASSWORD` — portal credentials for Playwright login
- Config home env var set to `/data/workspace/.config` — gog Gmail/Sheets access
- `GOG_KEYRING_PASSWORD=ferdybot-calendar-2026` — gog keyring
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — for notifications and 2FA code requests

### Google Sheets
- Sheet ID: `1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk`
- Tab: `2026`

### Telegram
- Chat ID: `8409634074`
