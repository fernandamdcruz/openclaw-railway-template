# GPS Boleto Monthly — Skill Definition

## Description
Generate monthly GPS (Guia da Previdência Social) boletos for Fernanda and Max via the SAL portal. Uses a Python script with Browserbase (cloud browser) + Playwright so Fernanda can solve CAPTCHAs via live view URL.

## Trigger
Say "GPS boleto", "gerar GPS", "boleto previdência", or similar. Also triggered by the monthly cron job on the 5th.

## Availability Check (cron trigger only)

When this skill is triggered by the **monthly cron job** (NOT when Fernanda asks manually):

1. **Ask first — do NOT run the script yet.**
   Send via Telegram (chat ID: 8409634074):
   ```
   GPS boleto time! Competência: [previous month/year].
   Preciso da sua ajuda para resolver o CAPTCHA no portal SAL.
   Agora é um bom momento? Responda:
   • "sim" — começo agora
   • "depois" — pergunto de novo em 12h
   • "dispensar" — cancelo o lembrete deste mês
   ```

2. **If Fernanda replies "sim" / "yes" / affirmative** → proceed to run the script below.

3. **If no response within 30 minutes, OR she replies "depois" / "later":**
   - Reply: "OK! Pergunto de novo em 12 horas."
   - Wait 12 hours, then re-send the availability message (step 1).

4. **Reminder loop**: Keep re-asking every 12 hours until she either says "sim" or "dispensar".

5. **If she replies "dispensar" / "dismiss" / "cancelar":**
   - Reply: "OK, GPS [month] não será gerado automaticamente. Diga 'GPS boleto' quando quiser gerar manualmente."
   - Stop all reminders for this month.

**When triggered manually** (Fernanda says "GPS boleto" in chat): skip this section entirely — she's available. Go straight to running the script.

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
