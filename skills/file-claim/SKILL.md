# File BCBS Claim — Skill Definition

## Trigger
"file claim", "file my claims", "submit reimbursement", "BCBS claim", or similar.

## What To Do

Run the Playwright script. It handles everything: login, 2FA, form filling, document upload, sheet update, and Telegram notification.

```bash
python3 /data/workspace/skills/file-claim/claim_filer.py
```

Report the script's output to Fernanda. That's it.

## If The Script Fails

1. Report the error output to Fernanda exactly as printed.
2. **STOP.** Do NOT try to fix the script, edit the code, or fall back to manual browser automation.
3. Wait for Fernanda's instructions.

> **Manual fallback exists** in `MANUAL_FALLBACK.md` (same directory), but **only read it if Fernanda explicitly tells you to continue manually.** Never read it on your own initiative.

## Provider → Patient Mapping

- CLINICA LIVIDI / Clínica Lividi Med → patient: **Elena Miranda** (never Mathias)

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
