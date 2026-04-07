# eSocial DAE Monthly — Skill Definition

## Description
Generate monthly eSocial DAE (Documento de Arrecadação do eSocial) payment slips for domestic workers, using Browserbase for browser automation with live view so Fernanda can handle gov.br authentication.

## Trigger
Say "eSocial", "DAE", "guia eSocial", "pagamento doméstica", or similar. Also triggered by monthly cron job.

## Availability Check (cron trigger only)

When this skill is triggered by the **monthly cron job** (NOT when Fernanda asks manually):

1. **Ask first — do NOT open any browser or start automation yet.**
   Send via Telegram (chat ID: 8409634074):
   ```
   eSocial DAE time! Competência: [previous month/year].
   Preciso que você faça login no gov.br pelo live view.
   Agora é um bom momento? Responda:
   • "sim" — começo agora
   • "depois" — pergunto de novo em 12h
   • "dispensar" — cancelo o lembrete deste mês
   ```

2. **If Fernanda replies "sim" / "yes" / affirmative** → proceed to the Workflow section below.

3. **If no response within 30 minutes, OR she replies "depois" / "later":**
   - Reply: "OK! Pergunto de novo em 12 horas."
   - Wait 12 hours, then re-send the availability message (step 1).

4. **Reminder loop**: Keep re-asking every 12 hours until she either says "sim" or "dispensar".

5. **If she replies "dispensar" / "dismiss" / "cancelar":**
   - Reply: "OK, eSocial DAE [month] não será gerado automaticamente. Diga 'eSocial DAE' quando quiser gerar manualmente."
   - Stop all reminders for this month.

**When triggered manually** (Fernanda says "eSocial" or "DAE" in chat): skip this section entirely — she's available. Go straight to Workflow.

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
