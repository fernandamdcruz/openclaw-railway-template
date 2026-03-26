# eSocial DAE Monthly — Skill Definition

## Description
Generate monthly eSocial DAE (Documento de Arrecadação do eSocial) payment slips for domestic workers, using Browserbase for browser automation with live view so Fernanda can handle gov.br authentication.

## Trigger
Say "eSocial", "DAE", "guia eSocial", "pagamento doméstica", or similar. Also triggered by monthly cron job.

## Workflow

### Setup
- Use browser profile `browserbase` (NOT the default `openclaw` profile)
- After creating the session, get the **live view URL** and send it to Fernanda via Telegram (chat ID: 8409634074)
- Message: "eSocial DAE time! Abra o live view para autenticação gov.br: [live view URL]"

### Step 1: Navigate to eSocial portal
- URL: `https://login.esocial.gov.br`
- This will redirect to gov.br authentication

### Step 2: Authentication (requires Fernanda)
- The gov.br login requires CPF + password or certificate
- Send Telegram message: "Preciso que você faça login no gov.br pelo live view: [live view URL]"
- Wait for Fernanda to complete authentication (up to 5 minutes)
- Once logged in, the portal will redirect to the eSocial dashboard

### Step 3: Navigate to DAE generation
- Look for "Empregador Doméstico" or similar section
- Navigate to the DAE/payment slip generation page
- This is typically under: Folha/Recebimentos e Pagamentos → Emitir DAE

### Step 4: Select competência
- Select the current month's competência (the month being paid for)
- Typically the PREVIOUS month — e.g., generating in April = competência March

### Step 5: Review and generate
- The system will show the DAE with all domestic workers registered
- Review the amounts
- Click to generate/emit the DAE
- Take a screenshot at each step

### Step 6: Extract payment info
- Look for "Código de Barras" or "Linha Digitável" — the payment barcode number
- Also capture: total amount and due date
- Take a screenshot of the completed DAE

### Step 7: Send via Telegram
- Send to chat ID 8409634074:
  ```
  ✅ eSocial DAE — [MM/YYYY]
  Linha digitável: [barcode number]
  Valor: R$ [total amount]
  Vencimento: [due date]
  ```

## Notes
- The gov.br portal requires human authentication — Fernanda MUST log in via the live view URL
- Browserbase live view lets her see and interact with the browser session in real-time
- After authentication, FerdyBot can navigate the remaining steps
- Always use browser profile `browserbase`, never `openclaw` for this skill
- The eSocial portal structure may vary — take screenshots at each step for debugging
- If the portal layout changes, coordinates/selectors may need recalibration
- Telegram delivery chat ID: 8409634074

## ⚠️ Important
- Fernanda needs to provide: employee names, CPF (employer), and any login credentials separately
- Do NOT store gov.br passwords in Railway env vars or skill files
- Authentication is always done by Fernanda via the Browserbase live view
