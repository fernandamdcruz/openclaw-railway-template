# FerdyBot — Railway/OpenClaw Reference

## Service Info
- **URL**: https://openclaw-production-744f.up.railway.app
- **Platform**: Railway → service `ec24cb01` in project `136819d0`
- **Volume**: `openclaw-volume` mounted at `/data`
- **Config file**: `/data/.openclaw/openclaw.json`
- **Healthcheck path**: `/setup/healthz` (timeout: 300s)
- **Docker start command**: `./entrypoint.sh` (set in Railway custom start command field — this runs Xvfb + Chromium + OpenClaw server)

---

## Current Config State (as of 2026-03-24)

The persistent volume config has (deployed via two-step procedure, confirmed working):
```json
{
  "agents": {
    "defaults": {
      "model": {
        "primary": "anthropic/claude-sonnet-4-5"
      },
      "compaction": {
        "reserveTokensFloor": 150000
      }
    }
  },
  "session": {
    "dmScope": "per-channel-peer",
    "idleMinutes": 30
  },
  "browser": {
    "enabled": true,
    "defaultProfile": "openclaw",
    "profiles": {
      "openclaw": {
        "cdpUrl": "http://127.0.0.1:9222",
        "color": "#000000"
      },
      "browserbase": {
        "cdpUrl": "wss://connect.browserbase.com?apiKey=${BROWSERBASE_API_KEY}",
        "color": "#000000"
      }
    }
  }
}
```

**What this config does:**
- **Model pinned** to `claude-sonnet-4-5` — future auto-updates cannot change it
- **Compaction at ~50k tokens** — `reserveTokensFloor: 150000` means auto-compaction triggers at 200k - 150k = 50k tokens (safety net)
- **Idle reset after 30 min** — uses legacy top-level `session.idleMinutes` path (confirmed correct; see Session Reset Research for why the nested `session.reset.idleMinutes` path does NOT work)

**Model is explicitly pinned.** Previously the model was `undefined` in the volume config, meaning OpenClaw used its built-in default. When OpenClaw auto-updated (2026-03-13) its default changed from Sonnet to Opus 4.6, causing rate limit failures. The model is now hardcoded so future auto-updates cannot change it.

---

## Environment Variables for Config (Research findings)

OpenClaw does NOT expose `session.reset.idleMinutes` or `agents.defaults.compaction.mode`
as official Railway environment variables.

**What IS supported via env vars:**
- `OPENCLAW_HOME` — override home directory
- `OPENCLAW_GATEWAY_TOKEN` — gateway auth token
- `OPENCLAW_STATE_DIR` — state directory
- `OPENCLAW_WORKSPACE_DIR` — workspace directory

**The config file is the only supported way** to set session timeout and compaction mode.

**Alternative approach** (if OpenClaw supports it):
You can use `${VAR_NAME}` substitution inside the JSON config:
```json
{
  "session": {
    "reset": {
      "idleMinutes": "${SESSION_IDLE_MINUTES}"
    }
  }
}
```
Then set `SESSION_IDLE_MINUTES=60` in Railway Variables.
This needs testing to confirm it works.

---

## Refreshing gog OAuth Token (when Google Sheets/Gmail stop working)

gog's OAuth token expires periodically. When this happens, the claim filing script fails at the very start because it can't read pending claims from Google Sheets.

**Symptom**: Script errors with OAuth/token/authentication failure when trying to access Sheets or Gmail.

**How to fix** (takes ~2 minutes):

1. Tell FerdyBot to run this command:
   ```
   GOG_KEYRING_PASSWORD=ferdybot-calendar-2026 XDG_CONFIG_HOME=/data/workspace/.config gog auth add fernanda.mdcruz@gmail.com --services user --manual
   ```

2. FerdyBot will reply with a long Google authorization URL starting with `https://accounts.google.com/o/oauth2/auth?...`

3. Open that URL in Safari on your Mac. Sign into Google and approve access.

4. Your browser will redirect to a URL starting with `http://127.0.0.1:XXXXX/oauth2/callback?...` — the page won't load (that's normal).

5. Copy the **entire URL** from Safari's address bar.

6. Paste that URL back to **FerdyBot** (not Cowork). FerdyBot's terminal is waiting for it.

7. FerdyBot should confirm the auth succeeded. You can then ask it to file claims again.

**Important**: gog is only installed inside the Railway container. You cannot run `gog` on your Mac. The auth flow must go through FerdyBot.

---

## Fix Script (tested and working)

This Node.js inline script was used to fix the config. It:
1. Removes any bad `compaction` key
2. Sets `session.reset.idleMinutes = 60`
3. Logs the result for verification

```bash
node -e "const fs=require('fs'),p='/data/.openclaw/openclaw.json';try{let c={};try{c=JSON.parse(fs.readFileSync(p,'utf8'))}catch(e){console.log('Read err:',e.message)};delete c.compaction;if(!c.session)c.session={};if(!c.session.reset)c.session.reset={};c.session.reset.idleMinutes=60;fs.writeFileSync(p,JSON.stringify(c,null,2));console.log('Config ok. session:',JSON.stringify(c.session),'compaction:',c.compaction)}catch(e){console.log('Fatal:',e.message)}"
```

**IMPORTANT**: To use this as a Railway Custom Start Command, append `; npm run start`
(NOT `; node server.js` — that breaks healthchecks).

⚠️ **WARNING**: Do NOT append `; npm run start` to the custom start command — this bypasses
the entrypoint and breaks the healthcheck. Instead, after running the script to fix the config,
**clear the custom start command field entirely** and trigger a fresh deploy. The volume write
persists even if the deploy fails, so the config will be picked up by the next clean deploy.

---

## BCBS Claim Filing Skill

FerdyBot can automatically file insurance reimbursement claims on the GeoBlue/BCBS member portal via direct API calls (no browser automation).

### Files
- **`skills/file-claim/SKILL.md`** — Skill definition. Tells FerdyBot to run the Python script (nothing else).
  - Source of truth: https://github.com/fernandamdcruz/openclaw-railway-template/blob/main/skills/file-claim/SKILL.md
  - Deploy: `entrypoint.sh` copies from `/app/skills/` to `/data/workspace/skills/` on container start
- **`skills/file-claim/claim_filer_api.py`** — Main claim filing script (~1500 lines). Handles everything: sheets reading, auth, API filing, doc upload, Telegram notifications.
  - Version tag: `api-v9-hardcode-all-defaults-2026-03-27`
- **`skills/file-claim/claim_filer.py`** — Legacy Playwright-based filer, now only used as fallback for 2FA code extraction from Gmail.
- **`skills/file-claim/test_file_claim.py`** — Standalone API test script. Bypasses sheets/auth, uses manual `BCBS_TOKEN` env var.

### How it works (API-based, as of 2026-03-27)
1. FerdyBot runs `python3 /data/workspace/skills/file-claim/claim_filer_api.py`
2. Script reads pending claims from Google Sheets (via `gog` CLI)
3. Script authenticates to BCBS Okta via Playwright → when 2FA hits, asks Fernanda on Telegram for the 6-digit code
4. Files each claim via REST API (`claimsapire.hthworldwide.com/v4`):
   - Step 1: POST /claimants/save/ (create claim + set claimant)
   - Step 2: POST /insurance/save/ (set other insurance = none)
   - Step 3: POST /charges/save/ (add charge with provider, amount, dates)
   - Step 4: Download receipt from Google Drive, upload via S3 presigned URL
   - Step 5: POST /paymentaccounts/save/ (set wire payment to saved account)
   - Step 6: POST /claims/submit (submit with signature + terms agreement)
5. Updates Google Sheets row status to "Filed" and writes claim reference number

### First successful end-to-end filing: 2026-03-27
Claim CLM-1066884 for Fernanda / Clínica Oftalmológica SW Ltda — filed and submitted successfully, confirmed by BCBS email.

### ⚠️ Reimbursement Method — ALWAYS WIRE, NEVER CHECK

**ALWAYS select WIRE as the reimbursement method. NEVER select CHECK.**

The pre-saved bank account on file is a US account (USD). PaymentAccountID: 141210.

### ⚠️ CRITICAL: All claims MUST have a receipt/document attached

Claims submitted without a supporting document (invoice/receipt) will be rejected by BCBS. The script downloads the file from Google Drive (column N) and uploads it to the claim. If document upload fails, the script should NOT submit the claim.

### Hardcoded defaults (no env vars needed)
The script hardcodes all defaults so it works without any env vars beyond what Railway already has:
- `GOOGLE_SHEET_ID` = `1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk`
- `GOG_CONFIG_DIR` = `/data/workspace/.config`
- `XDG_CONFIG_HOME` = `/data/workspace/.config`
- `GOG_ACCOUNT` = `fernanda.mdcruz@gmail.com`
- `GOG_KEYRING_PASSWORD` = `ferdybot-calendar-2026`
- Telegram bot token read from `/data/.openclaw/openclaw.json` → `channels.telegram.botToken`

### Google Sheets Data Source

**Sheet ID**: `1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk`
**Tab**: `2026`
**URL**: https://docs.google.com/spreadsheets/d/1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk/edit

FerdyBot writes rows to this sheet when processing medical invoices. When filing claims on BCBS, it must read the exact values from this sheet — **never guess, never use placeholder values**.

#### Column → BCBS Form Field Mapping

| Column | Sheet Header | BCBS Form Field | Notes |
|--------|-------------|-----------------|-------|
| A | Date Processed | (metadata only) | When FerdyBot processed the invoice |
| B | Patient Name | Step 2: Patient dropdown | Must match exactly the BCBS enrolled dependent name |
| C | Provider Name | Step 4: Select Provider | Search autocomplete — use exact name to find match |
| D | Date of Service | Step 4: Start Date + End Date | Use same date for both if single-day visit |
| E | Amount Billed | Step 4: Charge Amount | Exact value, no rounding |
| F | Currency | Step 4: Billed Invoice Currency | BRL = Brazilian Real, EUR = Euro, USD = US Dollar |
| G | Diagnosis Codes | Step 4: Condition or Diagnosis | CID/ICD codes — search in BCBS dropdown; use OTHER if not found |
| H | Procedure Codes | Step 4: Service Description | Map to closest BCBS dropdown option; use OTHER if not found |
| I | Invoice # | Step 4: Charge Nickname | Use as the charge nickname (e.g. "INV-5202") |
| J | Year | (metadata only) | Year of service — for reference only |
| K | City | Step 4: City | City where treatment occurred |
| L | Country | Step 4: Country of Treatment | Country where treatment occurred |
| M | Claim Status | (filter + update) | Only process rows where status = "Pending"; update to "Filed" after submission |
| N | Drive File Link | Step 4: Supporting Document | **Use the original invoice file already in the Telegram chat — no Drive download needed** |
| O | Bill Type | (metadata only) | "Medical", "Dental", etc. — for reference only |
| P | Secondary Doc | (optional) | Secondary supporting document if applicable |
| Q | Claim Ref # | (auto-filled after filing) | BCBS reference number — written by the filing script |
| R | Notes | (metadata only) | Free text notes |

**When writing new rows to the sheet** (processing incoming medical bills), FerdyBot MUST populate ALL columns A through R. Leave cells blank if the value is unknown — never skip columns or shift data. The column order is critical for the automated claim filing script.

#### Supporting Document Upload (Column N)

Each row has a Google Drive file link in column N — this is the scanned invoice/receipt that FerdyBot originally received via Telegram and then uploaded to Drive to generate that link. FerdyBot must:
1. **Use the original file from the Telegram chat** — it's already in context (this is the same file that was used to create the Drive link in column N). No need to download from Drive.
2. Upload it to the BCBS supporting documents step for that charge
3. Do NOT use placeholder files — the actual invoice is required for the claim to be processed

#### Provider → Patient Assignment

Providers are tied to specific patients. Do NOT mix them up. Known mappings (confirm against sheet):
- **CLINICA LIVIDI / Clínica Lividi Med** → patient: **Elena Miranda**. **Never file under Mathias.**
- **Dr. Rohrmoser** → can be either **Fernanda** or **Mathias** — check the Patient Name in column B

If any required field (B, C, D, E, F, K, L) is blank in the sheet row, **stop and ask** before filing. Do not invent values.

### Triggering
Say "file claim", "file my claims", "submit reimbursement", or similar to FerdyBot in Telegram.

### Railway env vars required
- `BCBS_USERNAME` — your BCBS member portal login email
- `BCBS_PASSWORD` — your BCBS member portal password

### Known fragility
The BCBS portal uses Flutter Web (HTML renderer, NOT CanvasKit canvas). The v2 skill uses **accessibility-based selectors** (`flt-semantics` elements with ARIA roles) instead of coordinate-based clicks, making it much more resilient to layout changes. Screenshots are taken at each step for debugging.

### Memory optimization (2026-03-13)
Previously the full Playwright script lived in `TOOLS.md` (loaded every session), contributing ~11 KB (~2,750 tokens) to every single session's context even when claims weren't being filed. Moving it to a skill file reduces the per-session baseline significantly. The skill is only loaded when FerdyBot decides it's relevant to the user's request.

---

## Docker Image: Chromium/Playwright Support

> **⚠️ UPDATE (2026-03-24): Local Chromium REINSTATED.** After a brief attempt to use Browserbase for everything (2026-03-23), local Chromium was brought back for BCBS claims. Browserbase's free tier (5 sessions/min) was unsustainable — OpenClaw creates multiple sessions per request, hitting rate limits. The paid plan ($100/mo) wasn't justified for one use case.

### Current setup (2026-03-24)
- **Forked** `arjunkomath/openclaw-railway-template` → `fernandamdcruz/openclaw-railway-template`
- **Dockerfile** conditionally installs Playwright Chromium + system dependencies when `OPENCLAW_INSTALL_BROWSER=1` (Railway build arg, currently set to `1`)
- **`entrypoint.sh`** starts Xvfb + Chromium with all required flags before launching OpenClaw server
- **Railway custom start command**: `./entrypoint.sh` (CRITICAL — Railway overrides ENTRYPOINT when a custom start command is set, so we must explicitly invoke it)

### How Chromium is launched
The `entrypoint.sh` script:
1. Starts Xvfb on display :99 (virtual framebuffer for headless Chromium)
2. Uses `find` to locate the Playwright Chromium binary (with multiple fallback paths)
3. Launches Chromium with: `--headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu --remote-debugging-port=9222`
4. OpenClaw connects via CDP at `http://127.0.0.1:9222` (the `openclaw` browser profile)

### Confirmed Chromium path (2026-03-24)
For `openclaw@2026.3.13`, Playwright installs `chromium-1208` (full Chrome). The actual binary path is:
```
/home/openclaw/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome
```
Note: the directory is `chrome-linux64`, NOT `chrome-linux` — this is why the old glob pattern (`chromium-*/chrome-linux/chrome`) failed. The `find`-based detection in `entrypoint.sh` handles any directory structure.

The ms-playwright directory contains three subdirectories: `chromium-1208/`, `chromium_headless_shell-1208/`, and `ffmpeg-1011/`.

### Previous issues (all now fixed)
The original local Chromium setup had reliability issues:
1. **`/dev/shm` too small** — fixed with `--disable-dev-shm-usage` flag
2. **Xvfb not started** — fixed: entrypoint.sh now launches Xvfb
3. **Missing `--no-sandbox`** — fixed: added to Chromium launch flags
4. **Chromium binary not found** — fixed: switched from glob pattern to `find` with fallback paths (commit `0c001b2`). The old glob used `chrome-linux` but the actual dir is `chrome-linux64`.

### ⚠️ IMPORTANT: Do NOT talk to FerdyBot from Cowork
Cowork-to-FerdyBot chat conversations burn API credits fast ($5-6/day). All FerdyBot interactions should be done by Fernanda directly via Telegram. Cowork should only read the OpenClaw dashboard UI — never send messages in the chat.

---

## Browser Architecture: Split Local + Cloud (2026-03-24)

**Two browser profiles for different use cases:**

| Profile | Use case | Type | Why |
|---------|----------|------|-----|
| `openclaw` (default) | BCBS claims | Local Chromium via CDP | No CAPTCHAs, no live view needed. Free, reliable. |
| `browserbase` | gov.br skills (GPS boleto, eSocial DAE) | Cloud browser with live view | Needs live view URL for Fernanda to solve CAPTCHAs and log into gov.br |

### Status: ✅ Deployed

- **Local Chromium**: `OPENCLAW_INSTALL_BROWSER=1`, Chromium starts in entrypoint.sh on CDP port 9222
- **Browserbase**: `BROWSERBASE_API_KEY` set as Railway env var (kept for gov.br skills)
- **Skill files**:
  - BCBS file-claim: `skills/file-claim/SKILL.md` → uses `openclaw` profile ✅
  - GPS Boleto: `/data/workspace/skills/gps-boleto/SKILL.md` → uses `browserbase` profile ✅
  - eSocial DAE: `/data/workspace/skills/esocial-dae/SKILL.md` → uses `browserbase` profile ✅

### Railway env vars
- `OPENCLAW_INSTALL_BROWSER=1` — build arg, triggers Chromium install in Docker build
- `BROWSERBASE_API_KEY` — for gov.br skills that need live view

### Skill files
- **BCBS file-claim**: `skills/file-claim/SKILL.md` in GitHub repo → target: `/data/workspace/skills/file-claim/SKILL.md`
- **GPS Boleto**: `gps-boleto-skill.md` → target: `/data/workspace/skills/gps-boleto/SKILL.md`
- **eSocial DAE**: `esocial-dae-skill.md` → target: `/data/workspace/skills/esocial-dae/SKILL.md`
- **Deploy method**: Host in GitHub repo (`skills/{name}/SKILL.md`), then send FerdyBot a single curl command to fetch and save.

### How the live view workflow works (gov.br skills only)
1. FerdyBot creates a Browserbase session (profile `browserbase`)
2. Gets the live view URL, sends it to Fernanda via Telegram
3. FerdyBot automates form filling
4. When CAPTCHA or gov.br login appears, Fernanda opens the URL and interacts
5. FerdyBot detects completion and continues automation
6. Extracts boleto codes, sends them via Telegram

---

## Deploying to Railway

### Auto-deploy (normal)
Railway auto-deploys from GitHub on push to `main`. However, the webhook can be unreliable — two commits on 2026-03-24 did NOT trigger auto-deploys (possibly related to Railway EU-West incident). Settings appeared correct (branch=main, Wait for CI=OFF).

### Manual deploy via GraphQL API (workaround)
When auto-deploy fails, use Railway's internal GraphQL API. This is what the Railway web UI itself uses.

**Endpoint**: `https://backboard.railway.com/graphql/internal` (NOT `backboard.railway.app/graphql/v2` — that endpoint returns "Not Authorized" for mutations)

**Auth**: Session cookies (must be run from browser console while logged into Railway)

**Deploy latest commit on main**:
```javascript
fetch('https://backboard.railway.com/graphql/internal', {
  method: 'POST',
  credentials: 'include',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    query: `mutation { serviceInstanceDeployV2(
      serviceId: "ec24cb01-...",
      environmentId: "41afa7c7-...",
      commitSha: "COMMIT_SHA_HERE"
    ) { id status } }`
  })
}).then(r => r.json()).then(console.log)
```

The `commitSha` parameter is optional but recommended — without it, Railway deploys whatever commit the service was last on (which may not be the latest).

**Key discovery**: Railway's "Redeploy" button rebuilds from the SAME commit as the original deployment. It does NOT pick up new commits from main. To deploy a new commit, you must either wait for auto-deploy or use the GraphQL API with the specific `commitSha`.

### Railway IDs
- Project ID: `136819d0-...`
- Service ID: `ec24cb01-...`
- Environment ID: `41afa7c7-...`

---

## Incident Log

### 2026-03-27: BCBS API claim filing — first successful end-to-end
- **What happened**: After days of failed Playwright-based browser automation attempts, switched to direct REST API calls (`claimsapire.hthworldwide.com/v4`). Tested locally via Railway CLI with a manually provided BCBS token. Discovered and fixed multiple issues: wrong API body structures (wrapper keys like `OtherInsuranceDetail` vs `Insurance`, `PaymentAccountDetail` vs `PaymentAccount`), missing env var defaults, Telegram bot token not found, 2FA code reuse.
- **Key fixes**: (1) All API request bodies now match exact structure from BCBS portal (full `make_claim_object()` in every step), (2) All env var defaults hardcoded in script, (3) Telegram creds read from OpenClaw config file, (4) 2FA code reuse prevention via `/tmp/bcbs_2fa_used_codes.txt`, (5) SKILL.md rewritten to stop FerdyBot from reading/analyzing the script instead of running it.
- **Result**: Claim CLM-1066884 filed and confirmed by email.
- **Lesson**: Local testing via Railway CLI (`railway shell` + `BCBS_TOKEN=<token> python3 test_file_claim.py`) was the breakthrough. Iterating locally took minutes vs hours of deploy cycles. See Lessons Learned #0.

### 2026-03-24: Gateway crash-loop — missing `color` field in browser profiles
- **Cause**: OpenClaw config validation requires a `color` field (string) on every browser profile. The volume config had profiles with only `cdpUrl`, missing the mandatory `color`. This caused the gateway to crash on startup with: `Invalid config at /data/.openclaw/openclaw.json: browser.profiles.openclaw.color: Invalid input: expected string, received undefined`
- **Symptom**: FerdyBot completely offline. Service would start, fail config validation, exit code=1, repeat (crash-loop). Railway showed "Completed ⚠" with healthcheck timeout.
- **Fix**: Used the two-step config fix procedure — set Railway custom start command to a Node.js one-liner that patched both profiles with `color: "#000000"`, deployed (config written to volume, deploy failed healthcheck as expected), then cleared the start command and deployed again with clean `./entrypoint.sh`. Second deploy succeeded, gateway started normally.
- **Lesson**: Every browser profile in `openclaw.json` MUST have a `color` field (any valid color string like `"#000000"`). This is a schema validation requirement — missing it crashes the gateway immediately.

### 2026-03-24: Auto-deploy webhook not triggering
- **Cause**: Unknown (possibly Railway EU-West incident). Two commits pushed to main did not trigger auto-deploys. GitHub webhooks page was empty — Railway uses GitHub App integration, not webhooks.
- **Fix**: Deployed manually via Railway GraphQL API (`serviceInstanceDeployV2` mutation with explicit `commitSha`)
- **Lesson**: "Redeploy" in Railway UI rebuilds the SAME commit, not the latest. Use GraphQL API to deploy specific commits when auto-deploy fails. The `/internal` endpoint works with session cookies; the `/v2` endpoint rejects mutations.

### 2026-03-13: Opus escalation + rate limit failure
- **Cause**: OpenClaw auto-updated via GitHub (PR #22) and its new default model changed to `claude-opus-4-6`. Since the model was never explicitly set in the volume config, FerdyBot silently switched to Opus.
- **Symptom**: Every request used Opus (73k input tokens each), hitting Opus rate limits immediately even with light usage.
- **Fix**: Explicitly wrote `anthropic/claude-sonnet-4-5` to `agents.defaults.model.primary` in the volume config. Model is now pinned.
- **Lesson**: The volume config only stores *overrides*. OpenClaw's built-in defaults apply for anything not explicitly set. Always pin the model explicitly.

### 2026-03-13: Two-step config fix procedure discovered
- **Problem**: Running a Node.js config script with `; npm run start` appended fails the healthcheck (bypasses entrypoint.sh).
- **Correct procedure**: (1) Set custom start command to the Node.js script only (no npm run start), (2) deploy — script runs and writes config, deploy fails healthcheck, (3) clear the start command field, (4) deploy again — entrypoint.sh runs with the updated config. Volume persists between deploys.

---

## Session Reset Research (2026-03-13, deep dive)

### What we verified from logs

The Anthropic Console logs proved that after a **77-minute idle gap** (14:49 → 16:06), token counts did NOT reset:

| Time | Input Tokens | Event |
|------|-------------|-------|
| 14:30:47 | 17,922 | After manual `/reset` — clean baseline |
| 14:47–14:49 | 19k → 26,578 | Nota fiscal processing (tool calls, memory writes) |
| *77 min idle* | — | Session.reset.idleMinutes: 60 should have fired |
| 16:06:10 | 26,474 | First call — nearly identical to pre-gap |

The 26,474 after the gap vs 26,578 before (104-token difference) confirms context was not cleared.

### What we found in the source code

The OpenClaw repo is at **https://github.com/openclaw/openclaw** (310k stars). Key files:
- `src/config/sessions/reset.ts` — session reset logic
- `docs/reference/session-management-compaction.md` — deep dive doc
- `docs/gateway/configuration-reference.md` — full config reference

**Finding 1: `mode: "idle"` must be explicit**

In `src/config/sessions/reset.ts`, the `resolveSessionResetPolicy` function (lines 98-119):
```typescript
const hasExplicitReset = Boolean(baseReset || sessionCfg?.resetByType);
const mode =
  typereset?.mode ??
  basereset?.mode ??
  (!hasExplicitReset && legacyIdleMinutes != null ? "idle" : DEFAULT_RESET_MODE);
```

When we set `session.reset.idleMinutes: 60`:
- `hasExplicitReset = true` (the `reset` object exists)
- `basereset.mode` = `undefined` (we didn't set it)
- Result: `mode = DEFAULT_RESET_MODE = "daily"` — **not "idle"!**

The `idleMinutes: 60` value is stored but the mode defaults to daily resets at 4am, not idle resets.

**Finding 2: How idle resets do work**

The freshness check in `resolveSessionFreshness` (lines 147-158) runs the idle check whenever `policy.idleMinutes != null`, regardless of mode. So the check runs — but the question is whether it's creating a truly fresh session.

**Finding 3: Memory file re-injection explains the baseline**

FerdyBot uses tools profile `"coding"` which includes `group:memory`. This tools group writes session notes to disk files. These files are re-injected at the start of every new session. This is why "fresh" sessions (after `/reset`) start at ~17-18k tokens rather than ~1k. After processing nota fiscais, FerdyBot wrote the receipt data to memory files — growing the baseline from 17,922 → ~26,474.

**Finding 4: `compaction.mode: "safeguard"` is NOT a valid config key**

Previous versions of this README mentioned `agents.defaults.compaction.mode: "safeguard"`. This is **incorrect** — OpenClaw's compaction settings are:
```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "reserveTokensFloor": 20000,
        "keepRecentTokens": 20000,
        "model": "optional-model-for-summarization"
      }
    }
  }
}
```
There is no `mode` key. The bad key was silently ignored (or may have caused issues).

**Finding 5: Compaction threshold is 180k tokens**

Auto-compaction triggers when: `contextTokens > contextWindow - reserveTokens`
- Sonnet context window: 200,000 tokens
- Default `reserveTokens` = `max(16384, reserveTokensFloor=20000)` = **20,000**
- **Trigger point: 180,000 tokens**

FerdyBot has never reached this — max observed is ~73k (during the Opus incident). So compaction has never fired automatically.

**Finding 6: `parentForkMaxTokens` is NOT relevant here**

The `parentForkMaxTokens: 100000` config controls forking when a *new Telegram thread* is created from a parent message in a group chat. It does NOT affect idle resets or DM sessions. Irrelevant for FerdyBot.

---

### The correct permanent fixes

**Fix A — Explicit idle mode for DMs** (recommended, do via two-step deploy):
```json
{
  "session": {
    "dmScope": "per-channel-peer",
    "resetByType": {
      "direct": { "mode": "idle", "idleMinutes": 30 }
    }
  }
}
```
`resetByType.direct` applies specifically to DMs. With explicit `mode: "idle"`, the source code will correctly enter idle mode (not fall back to `DEFAULT_RESET_MODE = "daily"`). Set to 30 minutes for more aggressive clearing.

**Fix B — Legacy `session.idleMinutes`** (simpler, possibly equally effective):
```json
{
  "session": {
    "dmScope": "per-channel-peer",
    "idleMinutes": 30
  }
}
```
The legacy top-level `session.idleMinutes` (NOT nested under `reset`) triggers a different code path: `hasExplicitReset = false`, so `mode` becomes `"idle"` automatically. Simpler config, same intent.

**Fix C — Earlier compaction trigger** (safety net, add to any of the above):
```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "reserveTokensFloor": 150000
      }
    }
  }
}
```
With `reserveTokensFloor: 150000`, the compaction trigger drops from 180k → **50k tokens**. This means if any session grows past 50k tokens, OpenClaw will automatically summarize and compact it. Good insurance regardless of whether idle reset works.

**Recommended combined config** (all three fixes together):
```json
{
  "agents": {
    "defaults": {
      "model": {
        "primary": "anthropic/claude-sonnet-4-5"
      },
      "compaction": {
        "reserveTokensFloor": 150000
      }
    }
  },
  "session": {
    "dmScope": "per-channel-peer",
    "idleMinutes": 30
  }
}
```

> Note: `idleMinutes` at top-level session (not under `reset`) is the legacy path which is confirmed to work. Use this over `session.reset.idleMinutes`.

### What actually works right now

Until these fixes are deployed, **manual `/reset`** in Telegram is the only reliable tool. Send `/reset` to FerdyBot:
- Before sending a batch of notas fiscais
- After any long session that felt heavy
- At the start of each day

---

## Lessons Learned

0. **🚨 TEST LOCALLY VIA RAILWAY CLI BEFORE DEPLOYING — the #1 lesson from 2026-03-27**

   The BCBS claim filing script went through ~15 deploy-fail-debug cycles over several days because every fix was deployed blind to the Railway server, then tested via FerdyBot, then debugged from FerdyBot's limited output. This wasted days and cost significant API credits.

   **What finally worked**: Install Railway CLI on your Mac (`npx @railway/cli`), link to the project, run `railway shell` to load Railway env vars locally, then run the Python script directly on your Mac. This gives you:
   - Instant feedback (no 9-minute Docker builds)
   - Full terminal output (not filtered through FerdyBot's summarization)
   - Ability to iterate rapidly (edit → run → see error → fix → run again in seconds)
   - Direct control over env vars (e.g. `BCBS_TOKEN=<token> python3 script.py`)

   **Setup (one-time)**:
   ```bash
   npx @railway/cli login        # Opens browser to authenticate
   npx @railway/cli link          # Link to the project
   npx @railway/cli shell         # Opens shell with Railway env vars loaded
   # Now run scripts directly:
   python3 skills/file-claim/claim_filer_api.py
   ```

   **For BCBS API testing**: Grab a Bearer token from DevTools (Network tab → any API request → Authorization header), then:
   ```bash
   BCBS_TOKEN="<token>" python3 skills/file-claim/test_file_claim.py
   ```
   Token expires in 10 minutes. This bypasses all auth/Playwright complexity and tests the API flow directly.

   **Rule**: Never deploy a fix you haven't tested locally first. The deploy-and-pray cycle is a trap — it feels like progress but wastes time and money. Local testing with Railway CLI is 100x faster.

1. **Pre-deploy commands cannot access the persistent volume** — the volume is only mounted
   for the main container, not during pre-deploy. Use the start command instead.

2. **Set the custom start command to `./entrypoint.sh`** — Railway's custom start command
   overrides the Dockerfile's ENTRYPOINT, not just CMD. If you set `npm run start` as the
   start command, it completely bypasses `entrypoint.sh` (no Xvfb, no Chromium, no volume
   setup). The fix is to set the start command to `./entrypoint.sh` explicitly.

3. **`openclaw config set` hangs without a running gateway** — Don't use it in the start
   command before the server is up. Use direct JSON manipulation (Node.js inline script) instead.

4. **Config keys must be verified before use**:
   - ✅ `session.idleMinutes` — valid (top-level legacy path, cleanly enters idle mode)
   - ✅ `session.resetByType.direct.idleMinutes` — valid (DM-specific idle reset)
   - ✅ `agents.defaults.compaction.reserveTokensFloor` — valid compaction threshold
   - ⚠️ `session.reset.idleMinutes` — technically valid but defaults mode to "daily" not "idle" (use legacy path instead)
   - ❌ `agents.defaults.compaction.mode` — NOT a valid key in current OpenClaw (was silently ignored)
   - ❌ `compaction.mode` — INVALID root-level key, causes crash loop

5. **Railway "Redeploy" does NOT pick up new commits** — it rebuilds the exact same commit. To deploy a new commit when auto-deploy is broken, use the Railway GraphQL API (`backboard.railway.com/graphql/internal`) with a `commitSha` parameter. The `/v2` endpoint rejects mutations; only `/internal` works with session cookies.

6. **Browser profiles REQUIRE a `color` field** — every profile in `browser.profiles` must include
   `"color": "#000000"` (or any valid color string). Without it, OpenClaw's config validator rejects
   the entire config and the gateway crash-loops with `exited code=1`. This is not documented in
   OpenClaw's config reference but is enforced by the schema validator at startup.

7. **Volume config persists across failed deploys** — if a start command writes to
   `/data/.openclaw/openclaw.json` and then the healthcheck fails, the written config
   still remains on the volume.

8. **🚨 OpenClaw exec is stateless — the one-command rule**

   Every command sent to FerdyBot via OpenClaw chat runs in a fresh, isolated exec context. There is NO persistent shell session. `/tmp` files, environment variables, anything written in a prior command is gone by the next one.

   This means any multi-step approach — "write chunk 1, then chunk 2, then combine" — **always silently fails**. The commands look valid, the logic is sound, but step 2 has no memory of step 1. You end up debugging the commands when the real problem is the stateless architecture.

   **The rule**: The only reliable way to get files onto the Railway persistent volume (`/data/workspace/`) is a **single atomic command** that does the whole job in one shot.

   **The proven pattern**: Host content in the GitHub repo (`fernandamdcruz/openclaw-railway-template`) or a GitHub Gist, then send FerdyBot a single `curl`-to-disk command. One command, one round trip, done.

   **Preferred: GitHub repo over Gist.** Store skill files in the `skills/` directory of the repo (e.g., `skills/file-claim/SKILL.md`). This is easier to maintain, version-controlled, and the raw URL is predictable:
   `https://raw.githubusercontent.com/fernandamdcruz/openclaw-railway-template/main/skills/{skill-name}/SKILL.md`

   **Red flag**: If you ever find yourself planning multi-step "write this, then write that" into OpenClaw — stop. Collapse it into one command, or use an external host as the intermediary.

---

## Prompt Caching (Verified 2026-03-13)

**OpenClaw has built-in prompt caching support for Anthropic models — and it may already be active.**

### How it works

Prompt caching means OpenClaw marks your system prompt with Anthropic's `cache_control: {"type": "ephemeral"}` flag. Anthropic then stores that prompt for 5 minutes. If the *exact same* prompt bytes are sent again within 5 minutes, Anthropic charges ~10x less (cache read rate vs. full input rate) and responds faster.

### The `cacheRetention` config key

Set this at the **per-agent level** (`agents.list[].cacheRetention`):

| Value | Behavior |
|-------|----------|
| `"none"` | Disables caching; you pay full input token rates every turn |
| `"short"` | 5-minute ephemeral cache (this is the **default** for Anthropic API key auth) |
| `"long"` | Reserved for longer TTLs if Anthropic adds them |

**FerdyBot likely already has `"short"` caching active** — OpenClaw auto-seeds this default when using an Anthropic API key and no `cacheRetention` is explicitly set.

### The critical problem: dynamic system prompts

Caching only works if the **exact same bytes** are sent each turn. If your system prompt contains any dynamic content (timestamps, session IDs, rotating instructions), the cache misses every single turn — meaning you pay a cache *write* premium ($1.25/MTok vs $1.00/MTok base) with zero benefit.

**Known OpenClaw issue**: [Bug: Anthropic prompt caching broken — Cache Read always 0](https://github.com/openclaw/openclaw/issues/19534) — this is commonly caused by dynamic content in system prompts.

### What to check

To know if caching is actually working for FerdyBot, look at the Anthropic API usage dashboard:
- **High `cacheWrite`, zero `cacheRead`** → cache is being written but missing (dynamic prompt issue)
- **`cacheRead` > 0** → caching is working
- **No cache fields at all** → caching may be disabled

### Action item

Before changing anything: check the Anthropic console usage page to see if you're getting cache reads. If `cacheRead` is always 0, the system prompt likely has dynamic content that needs to be made static for caching to help.

---

## How to Make Future Config Changes

Use this two-step procedure (learned the hard way):

**Step 1**: Set the Railway custom start command to the Node.js script only:
```
node -e "const fs=require('fs'),p='/data/.openclaw/openclaw.json';try{let c={};try{c=JSON.parse(fs.readFileSync(p,'utf8'))}catch(e){console.log('Read err:',e.message)};/* YOUR CHANGES HERE */;fs.writeFileSync(p,JSON.stringify(c,null,2));console.log('Config written:',JSON.stringify(c))}catch(e){console.log('Fatal:',e.message)}"
```
Deploy. The script will run, write to the volume, then the deploy will fail healthcheck — that's expected and fine.

**Step 2**: Clear the custom start command field entirely. Deploy again. This time the entrypoint runs normally and picks up the updated config.

### Full recommended config script (use in Step 1):

This applies the correct permanent fix: pinned model + early compaction at 50k + legacy idle reset at 30min.

```
node -e "const fs=require('fs'),p='/data/.openclaw/openclaw.json';try{let c={};try{c=JSON.parse(fs.readFileSync(p,'utf8'))}catch(e){console.log('Read err:',e.message)};delete c.compaction;if(!c.agents)c.agents={};if(!c.agents.defaults)c.agents.defaults={};if(!c.agents.defaults.model)c.agents.defaults.model={};c.agents.defaults.model.primary='anthropic/claude-sonnet-4-5';if(!c.agents.defaults.compaction)c.agents.defaults.compaction={};c.agents.defaults.compaction.reserveTokensFloor=150000;delete c.agents.defaults.compaction.mode;if(!c.session)c.session={};c.session.idleMinutes=30;delete c.session.reset;if(c.browser&&c.browser.profiles){Object.values(c.browser.profiles).forEach(p=>{if(!p.color)p.color='#000000'})}fs.writeFileSync(p,JSON.stringify(c,null,2));console.log('Config ok:',JSON.stringify({model:c.agents.defaults.model,compaction:c.agents.defaults.compaction,session:c.session}))}catch(e){console.log('Fatal:',e.message)}"
```

What this script does:
- Pins model to `claude-sonnet-4-5`
- Sets `compaction.reserveTokensFloor: 150000` → compaction fires at ~50k tokens
- Sets legacy `session.idleMinutes: 30` (top-level, not nested) → idle resets after 30 min
- Ensures every browser profile has a `color` field (prevents gateway crash-loop)
- Removes old `compaction.mode` key (it was invalid)
- Removes old `session.reset` block (replaced by legacy path)

---

## 2FA Debugging History (2026-04-10)

### The Problem

The BCBS claim filing script (`skills/file-claim/claim_filer_api.py`) automates login via Okta SSO, which requires email-based 2FA. The script needs to obtain a 6-digit verification code sent to Gmail, enter it into the Okta form, and capture the resulting OAuth token.

### What Was Tried (and Why Each Failed)

#### Attempt 1: Gmail Auto-Extraction via `gog` CLI
- **Approach**: Use `gog gmail search` + `gog gmail get` to find the 2FA email and extract the code
- **Failures**:
  - `gog` CLI ignores `after:{epoch}` time filters — returns emails from hours ago
  - `newer_than:2m` filter also unreliable — old emails still returned
  - Gmail threads group ALL BCBS verification emails together (including trashed ones), so `gog gmail get {threadId}` returns every code ever sent
  - Added used-codes tracking (`/data/.openclaw/bcbs_2fa_used_codes.txt`) and `-in:trash -in:spam` filters, but stale codes still leaked through
  - The `stale_only_count` bail-out (originally 3 attempts = ~30s) was too aggressive — fresh email hadn't arrived yet

#### Attempt 2: Telegram File-Based Handoff with FerdyBot
- **Approach**: Script creates `/tmp/.bcbs_waiting_for_2fa`, sends Telegram message asking for code, FerdyBot sees the user's reply and writes code to `/tmp/.bcbs_2fa_code`, script reads from file
- **Failure**: FerdyBot is **blocked** waiting for the Python subprocess to complete. While the script runs, FerdyBot cannot process Telegram messages, so it never writes the code to the file. This is a fundamental architectural limitation — the script and FerdyBot share the same execution context.

#### Attempt 3: Direct Telegram `getUpdates` Polling
- **Approach**: Script polls Telegram's `getUpdates` API directly during execution
- **Initial concern**: Race condition with FerdyBot's own polling — `getUpdates` is destructive (whichever consumer calls first "claims" the messages)
- **Realization**: During script execution, FerdyBot IS blocked, so there's no race. The script is the only consumer.
- **Outcome**: Added to `ask_telegram_for_2fa()` — this approach is architecturally sound but was added alongside the Gmail extraction which kept failing before it could be reached.

#### Attempt 4: `asyncio.to_thread()` Fix
- **Problem discovered**: `get_2fa_code_from_gmail()` uses `time.sleep()` inside an async function, blocking the entire event loop. Playwright's response listeners (which capture the OAuth token) cannot fire while the event loop is frozen.
- **Fix**: Wrapped all sync calls in `asyncio.to_thread()` so they run in a thread pool
- **Outcome**: Fixed the event loop blocking, but Gmail extraction still returned stale codes

### Current State (2026-04-10)

**Gmail auto-extraction has been removed from the auth flow.** The script now:
1. Detects 2FA prompt on the Okta page
2. Immediately asks the user for the code via Telegram (direct `getUpdates` polling)
3. User replies with 6-digit code on Telegram
4. Script enters the code and submits
5. If rejected, asks for a new code (one retry)

The Gmail extraction code still exists in `claim_filer.py` but is no longer imported or called.

### Other Fixes Made During This Session

- **Secondary document upload**: Column P (`secondary_doc`) from Google Sheets was being parsed but never uploaded. Added Step 4b to upload secondary documents after the primary doc.
- **"Keep me signed in" interstitial**: Okta shows this page after 2FA — script now detects and clicks through it.
- **Multiple login buttons**: BCBS landing page has 2 "Login" buttons — fixed with `.first.click()`.
- **Runtime pip install anti-pattern**: Scripts were running `pip install` at startup. Fixed by adding `requests PyMuPDF` to the Dockerfile and replacing auto-install with clean error + exit.
- **Used codes file in /tmp**: Was wiped on every Railway redeploy. Moved to persistent `/data/.openclaw/` volume.

#### Root Cause Found (2026-04-10 evening): getUpdates Race with OpenClaw

OpenClaw's gateway uses **grammY long polling** (`getUpdates`) for Telegram — NOT webhooks. When our script also calls `getUpdates`, there's a race condition: OpenClaw's polling loop consumes the user's 2FA code message before our script sees it. The April 7 success was lucky timing — the script happened to win the race.

**Fix**: Before polling for the 2FA code, SIGSTOP the OpenClaw gateway process (freezes it without killing). After getting the code (or timeout), SIGCONT to resume. This makes our script the sole `getUpdates` consumer. The gateway resumes seamlessly — grammY handles reconnection automatically.

### Future: If Revisiting Gmail Auto-Extraction

If you want to try auto-extraction again, the key problems to solve are:
1. **Gmail thread grouping**: All BCBS verification emails land in the same thread. Need to either use individual message IDs (not thread IDs) or find a way to get gog to return only the newest message in a thread.
2. **Reliable time filtering**: gog CLI doesn't honor `after:` or `newer_than:` reliably. May need to use Gmail API directly via OAuth instead of gog.
3. **Code timing**: The 2FA email takes 30-60 seconds to arrive. Any extraction approach must wait at least 60 seconds before bailing.
4. **Event loop**: Any sync blocking code (time.sleep) MUST be wrapped in `asyncio.to_thread()` to keep Playwright's listeners alive.
