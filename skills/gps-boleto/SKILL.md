# GPS Boleto Monthly — Skill Definition

## Description
Generate monthly GPS (Guia da Previdência Social) boletos for Fernanda and Max via the SAL portal. Uses a Python script with Browserbase (cloud browser) + Playwright so Fernanda can solve CAPTCHAs via live view URL.

## Trigger
Say "GPS boleto", "gerar GPS", "boleto previdência", or similar. Also triggered by the monthly cron job on the 5th.

## 🛑 STOP — READ THIS FIRST IF TRIGGERED BY CRON

**If you got here from the monthly cron job (NOT a direct chat message from Fernanda):**

❌ **DO NOT run the Python script yet.**
❌ **DO NOT open a browser.**
❌ **DO NOT navigate to the SAL portal.**
❌ **DO NOT create a Browserbase session.**

Doing any of the above BEFORE Fernanda confirms availability **burns Browserbase minutes from our 60/month budget for nothing**, because the CAPTCHA times out without her.

### Step 1: Send the availability message via Telegram

You **CAN** send Telegram messages from cron context. Anyone who claims otherwise is wrong. Use this exact bash command:

```bash
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=8409634074" \
  -d "text=GPS boleto time! Competência: [PREVIOUS_MONTH/YEAR]. Preciso da sua ajuda para resolver o CAPTCHA no portal SAL. Agora é um bom momento? Responda: 'sim' (começo agora), 'depois' (pergunto em 12h), ou 'dispensar' (cancelo este mês)."
```

Replace `[PREVIOUS_MONTH/YEAR]` with the actual competência (e.g. `04/2026` if running in May).

### Step 2: STOP the cron job here.

Do **not** wait, do **not** poll, do **not** run anything else. Exit the cron job after sending the message. When Fernanda replies in Telegram, that triggers a NEW conversation where you'll handle her response.

### Step 3 (in the NEW conversation when Fernanda replies):

- **"sim" / "yes" / "vamos" / affirmative** → run the Python script (see "How to Run" below)
- **"depois" / "later" / "mais tarde"** → reply "OK! Pergunto de novo em 12h." and stop. (A future cron will re-ask.)
- **"dispensar" / "cancelar" / "skip"** → reply "OK, GPS [month] cancelado. Diga 'GPS boleto' quando quiser gerar manualmente." and stop.

**When triggered manually** (Fernanda types "GPS boleto" in chat): skip this entire STOP section — she's already available and confirmed by virtue of asking. Go straight to running the script.

## CRITICAL: Browserbase Budget

We have **60 free Browserbase minutes/month**. Each script run creates a session that burns minutes from the moment it starts — even if it fails or idles. Do NOT run the script until Fernanda has confirmed she is available ("sim"). A wasted session means fewer minutes for the real run.

## How to Run

**Just run the Python script. Do not try to use the browser directly.**

```bash
python3 /data/workspace/skills/gps-boleto/gps_boleto.py
```

The script handles everything:
- Creates a Browserbase session and sends the live view URL to Fernanda via Telegram
- Connects Playwright to the cloud browser
- Fills the SAL portal form for each person (Fernanda and Max)
- Pauses at CAPTCHA and notifies Fernanda to solve it via live view
- Extracts the linha digitável and sends results via Telegram

### Optional arguments
```bash
# Explicit competência (default: previous month)
python3 /data/workspace/skills/gps-boleto/gps_boleto.py --competencia 03/2026

# Generate for one person only
python3 /data/workspace/skills/gps-boleto/gps_boleto.py --person fernanda
```

## Do NOT
- Do NOT try to use the browser tool directly — use the Python script
- Do NOT read or analyze the script — just run it
- Do NOT modify the script at runtime

## Data Reference

| Person | NIT | Category | Code | Rate |
|--------|-----|----------|------|------|
| Fernanda | 11975199574 | Contribuinte Individual | 1163 | 11% |
| Max | 13883306818 | Contribuinte Individual | 1163 | 11% |

## Notes
- Competência is ALWAYS the previous month (paying in April = competência March)
- Code 1163 = 11% reduced rate over minimum wage (salário mínimo)
- The SAL portal has reCAPTCHA v2 — Fernanda solves via Browserbase live view
- Screenshots are saved to /tmp/gps_*.png for debugging
- Telegram delivery chat ID: 8409634074
