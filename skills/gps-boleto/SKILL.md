# GPS Boleto Monthly — Skill Definition

## Description
Generate monthly GPS (Guia da Previdência Social) boletos for Fernanda and Max via the SAL portal, using Browserbase for browser automation with live view so Fernanda can solve CAPTCHAs.

## Trigger
Say "GPS boleto", "gerar GPS", "boleto previdência", or similar. Also triggered by the monthly cron job on the 5th.

## Workflow

### Setup
- Use browser profile `browserbase` (NOT the default `openclaw` profile)
- After creating the session, get the **live view URL** and send it to Fernanda via Telegram (chat ID: 8409634074) so she can watch and interact

### For each person (Fernanda first, then Max):

**Step 1: Navigate to SAL portal**
- URL: `https://sal.rfb.gov.br/calculo-contribuicao/contribuintes-2`
- Wait for page to fully load

**Step 2: Fill initial form**
- Select category: **Contribuinte Individual**
- Enter NIT:
  - Fernanda: `11975199574` (formatted by portal as 119.75199.57-4)
  - Max: `13883306818` (formatted by portal as 138.83306.81-8)

**Step 3: CAPTCHA**
- Click the reCAPTCHA "Não sou um robô" checkbox
- If an image challenge appears, send Telegram message: "CAPTCHA apareceu! Abra o live view e resolva: [live view URL]"
- Wait up to 3 minutes for Fernanda to solve it
- Once CAPTCHA is solved (green checkmark visible), proceed

**Step 4: Click Consultar**
- The "Consultar" button should now be active
- Click it and wait for the next page to load

**Step 5: Select payment code**
- Find "Código de Pagamento" field
- Select **1163** (Contribuinte Individual — Recolhimento Mensal — NIT/PIS/PASEP — 11%)

**Step 6: Set competência**
- Find "Competência" field
- Enter the PREVIOUS month in MM/YYYY format
- Example: if running in April 2026, enter `03/2026`

**Step 7: Calculate**
- If the form asks for "Salário de Contribuição", it should auto-fill based on code 1163 (minimum wage)
- Click "Calcular" or "Gerar Guia"

**Step 8: Extract boleto code**
- Look for "Linha Digitável" — this is the 48-digit payment barcode number
- Also capture: amount (valor) and due date (vencimento)
- Take a screenshot of the boleto page

**Step 9: Send via Telegram**
- Send to chat ID 8409634074:
  ```
  ✅ GPS [Fernanda/Max] — [MM/YYYY]
  Linha digitável: [48-digit code]
  Valor: R$ [amount]
  Vencimento: [due date]
  ```

### After both boletos:
- Send summary message with both codes together

## Data Reference

| Person | NIT | Category | Code | Rate |
|--------|-----|----------|------|------|
| Fernanda | 11975199574 | Contribuinte Individual | 1163 | 11% |
| Max | 13883306818 | Contribuinte Individual | 1163 | 11% |

## Notes
- Competência is ALWAYS the previous month (paying in April = competência March)
- Code 1163 = 11% reduced rate over minimum wage (salário mínimo)
- The SAL portal has reCAPTCHA v2 — Browserbase's built-in CAPTCHA solving may handle it automatically. If not, Fernanda solves via live view.
- Always use browser profile `browserbase`, never `openclaw` for this skill
- Telegram delivery chat ID: 8409634074
