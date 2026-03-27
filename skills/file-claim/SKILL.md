# File BCBS Claim — Skill Definition

## Trigger
"file claim", "file my claims", "submit reimbursement", "BCBS claim", or similar.

## What To Do

Run the Playwright script. It handles everything: login, 2FA, form filling, document upload, sheet update, and Telegram notification.

```bash
python3 /data/workspace/skills/file-claim/claim_filer.py
```

**CRITICAL: As soon as the script finishes (whether it succeeded or failed), IMMEDIATELY message Fernanda with the result.** Do not wait. Do not do anything else first. She is waiting for your reply. Look for the `[RESULT]` line at the end of the script output and send that to her, plus a brief summary of what happened.

## If The Script Fails

1. **IMMEDIATELY message Fernanda** with the error output.
2. **STOP.** Do NOT try to fix the script, edit the code, or fall back to manual browser automation.
3. Wait for Fernanda's instructions.

> **Manual fallback exists** in `MANUAL_FALLBACK.md` (same directory), but **only read it if Fernanda explicitly tells you to continue manually.** Never read it on your own initiative.

## Provider → Patient Mapping

- CLINICA LIVIDI / Clínica Lividi Med → patient: **Elena Miranda** (never Mathias)
- Dr. Rohrmoser → can be either **Fernanda** or **Mathias** — always check the Patient Name in column B of the sheet

## Data Reference

### Env vars (set in Railway)
- `BCBS_USERNAME` / `BCBS_PASSWORD` — portal credentials
- Config home env var set to `/data/workspace/.config` — gog Gmail/Sheets access
- `GOG_KEYRING_PASSWORD=ferdybot-calendar-2026` — gog keyring

### Google Sheets
- Sheet ID: `1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk`
- Tab: `2026`

### Telegram
- Chat ID: `8409634074`
