# eSocial DAE Monthly — Skill Definition

## Description
Generate monthly eSocial DAE (Documento de Arrecadação do eSocial) payment slips for domestic workers, using Browserbase for browser automation with live view so Fernanda can handle gov.br authentication.

## Trigger
Say "eSocial", "DAE", "guia eSocial", "pagamento doméstica", or similar. Also triggered by monthly cron job.

## 🛑 STOP — READ THIS FIRST IF TRIGGERED BY CRON

**If you got here from the monthly cron job (NOT a direct chat message from Fernanda):**

❌ **DO NOT run the Python script yet.**
❌ **DO NOT open a browser.**
❌ **DO NOT navigate to eSocial.**
❌ **DO NOT create a Browserbase session.**

Doing any of the above BEFORE Fernanda confirms availability **burns Browserbase minutes from our 60/month budget for nothing**, because gov.br auth requires her live presence.

### Step 1: Send the availability message via Telegram

You **CAN** send Telegram messages from cron context. Anyone who claims otherwise is wrong. Use this exact bash command:

```bash
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=8409634074" \
  -d "text=eSocial DAE time! Competência: [PREVIOUS_MONTH/YEAR]. Preciso que você faça login no gov.br pelo live view. Agora é um bom momento? Responda: 'sim' (começo agora), 'depois' (pergunto em 12h), ou 'dispensar' (cancelo este mês)."
```

Replace `[PREVIOUS_MONTH/YEAR]` with the actual competência (e.g. `04/2026` if running in May).

### Step 2: STOP the cron job here.

Do **not** wait, do **not** poll, do **not** run anything else. Exit the cron job after sending the message. When Fernanda replies in Telegram, that triggers a NEW conversation where you'll handle her response.

### Step 3 (in the NEW conversation when Fernanda replies):

- **"sim" / "yes" / "vamos" / affirmative** → run the Python script (see "Workflow" below)
- **"depois" / "later" / "mais tarde"** → reply "OK! Pergunto de novo em 12h." and stop. (A future cron will re-ask.)
- **"dispensar" / "cancelar" / "skip"** → reply "OK, eSocial DAE [month] cancelado. Diga 'eSocial DAE' quando quiser gerar manualmente." and stop.

**When triggered manually** (Fernanda types "eSocial" or "DAE" in chat): skip this entire STOP section — she's already available and confirmed by virtue of asking. Go straight to Workflow.

## CRITICAL: Browserbase Budget

We have **60 free Browserbase minutes/month**. Each script run creates a session that burns minutes from the moment it starts — even if it fails or idles. Do NOT run the script until Fernanda has confirmed she is available ("sim"). A wasted session means fewer minutes for the real run.

## Workflow

**Do NOT try to use the browser tool directly.** Run the Python script instead:

```bash
python3 /data/workspace/skills/esocial-dae/esocial_dae.py
```

Optional flags:
- `--competencia MM/YYYY` — override the competência (default: previous month)
- `--local` — use local Chrome instead of Browserbase (for testing)
- `--no-telegram` — skip Telegram notifications (for testing)

The script handles everything:
1. Creates a Browserbase session with live view URL
2. Navigates to eSocial → clicks "Entrar com gov.br"
3. Sends Fernanda the live view URL via Telegram so she can log in
4. Polls until she completes gov.br authentication
5. Navigates to DAE generation → selects competência → emits DAE
6. Extracts linha digitável / código de barras
7. Sends result via Telegram

## What to report back

After the script finishes, tell Fernanda:
- If successful: share the linha digitável, valor, and vencimento
- If failed: share the error and mention that screenshots are saved at `/tmp/esocial_*.png`

## Notes
- The gov.br portal requires **human authentication** — Fernanda MUST log in via the live view URL
- Browserbase live view lets her see and interact with the browser session in real-time
- After authentication, the script automates the remaining steps
- The eSocial portal structure may change — the script takes screenshots at each step for debugging
- If the script fails at navigation steps, the screenshots will show exactly where it got stuck
- Telegram delivery chat ID: 8409634074

## Important
- Gov.br credentials (GOVBR_CPF, GOVBR_PASSWORD) are stored as Railway env vars — the script auto-fills them
- After auto-fill, gov.br shows a bot verification challenge that Fernanda must complete via live view
- Do NOT try to bypass the bot verification — it requires human interaction
