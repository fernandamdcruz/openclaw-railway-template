# FerdyBot — Personal AI Assistant for Fernanda

Telegram-based assistant that handles Fernanda's administrative chores: filing medical insurance claims, generating Brazilian tax/social-security boletos, and processing receipts.

Built on **OpenClaw** (an agent framework) deployed on **Railway**. Talks to Telegram, runs Python skills, drives browsers when needed.

---

## What FerdyBot does

| Skill | What it does | When it runs |
|-------|--------------|--------------|
| `file-claim` | Files BCBS/GeoBlue insurance reimbursement claims via the BCBS REST API. Reads pending claims from Google Sheets, handles 2FA via Telegram, updates Sheets with claim refs. | On demand: "file claim" in Telegram |
| `gps-boleto` | Generates monthly GPS (social-security) boletos for Fernanda and Max via the SAL portal. Uses Browserbase cloud browser for CAPTCHA solving. | Monthly cron (5th of month) or on demand: "GPS boleto" |
| `esocial-dae` | Generates monthly eSocial DAE (domestic-worker tax) slips via gov.br. Uses Browserbase for human gov.br login. | Monthly cron or on demand: "eSocial DAE" |
| Receipt processing | When Fernanda sends a bill image via Telegram: extracts data via vision, uploads to Drive, appends a row to the "Medical Bills" Google Sheet. | Automatic on image receipt (see `TOOLS.md`) |
| Calendar invites | Creates Google Calendar events via the `gog` CLI. | On demand: "create an event…" |

---

## Architecture

```
Telegram → OpenClaw Gateway (Railway) → Claude Sonnet 4.5 → Python skills
                    │                                        │
                    └──── Express wrapper (src/server.js) ───┘
                                Setup wizard at /setup
                                Web TUI at /tui
```

**Single Docker container on Railway, single persistent volume at `/data`.**

- `src/server.js` — Express wrapper. Proxies traffic to internal OpenClaw gateway, injects auth tokens, manages gateway lifecycle.
- `entrypoint.sh` — Container startup. Deploys skills + workspace docs to `/data/workspace`, launches Chromium for local browser tasks.
- `Dockerfile` — Builds the image (installs OpenClaw via npm + Python deps + Playwright).
- `skills/*/SKILL.md` — Skill definitions injected into FerdyBot's context. Each defines a trigger phrase and the command to run.

---

## 📍 Where to make common edits

### Change a skill's behavior (BCBS claim, GPS, eSocial)

Each skill lives in `skills/<name>/`:

```
skills/file-claim/
  ├── SKILL.md              ← trigger phrases + what FerdyBot should do (loaded every call)
  ├── claim_filer_api.py    ← the actual logic (BCBS REST API, 2FA, Sheets updates)
  ├── MANUAL_FALLBACK.md    ← reference doc for when the API script fails (not auto-loaded)
  └── _dev/                 ← debugging utilities, not part of runtime
```

To change WHAT the script does, edit the `.py` file. To change WHEN FerdyBot triggers it or HOW it explains itself to FerdyBot, edit `SKILL.md`.

### Change the receipt-processing flow

Edit `TOOLS.md` (the workflow steps + provider-to-Sheets mapping) or `receipt_sheet_template.py` (the Python snippet that appends a row to the Sheet).

### Change deployment config (model, sessions, compaction)

Live config lives on the Railway volume at `/data/.openclaw/openclaw.json` — NOT in this repo. Edit via Railway SSH (see `docs/FERDY_README.md` → "How to Make Future Config Changes" for the safe two-step deploy pattern).

### Change setup wizard / web TUI

Edit files in `src/public/` (HTML/CSS/JS, no build step).

---

## Deployment

- **Platform:** Railway, project `vivacious-compassion`, service `OpenClaw`
- **Auto-deploy:** push to `main` → Railway rebuilds Docker image + redeploys

```bash
cd ~/openclaw-railway-template
git push origin main
# Then watch:
railway logs
```

To open a shell in the live container:
```bash
railway ssh
```

To force a fresh deploy (no code changes):
```bash
railway redeploy
```

---

## Environment variables (Railway → Variables)

| Variable | Purpose |
|----------|---------|
| `SETUP_PASSWORD` | Protects the `/setup` wizard |
| `OPENCLAW_STATE_DIR` | `/data/.openclaw` (config + credentials) |
| `OPENCLAW_WORKSPACE_DIR` | `/data/workspace` (skills + memory) |
| `OPENCLAW_GATEWAY_TOKEN` | Auto-generated if unset; auto-injected into proxied requests |
| `BCBS_USERNAME` / `BCBS_PASSWORD` | BCBS portal login (used by `claim_filer_api.py`) |
| `BROWSERBASE_API_KEY` | Cloud browser for gov.br + SAL skills |
| `GOVBR_CPF` / `GOVBR_PASSWORD` | Pre-fill credentials for gov.br auth |
| `GOG_KEYRING_PASSWORD` | Encrypts `gog` CLI's Google OAuth tokens on disk |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Notifications from Python scripts |

---

## Repo layout

```
openclaw-railway-template/
├── CLAUDE.md                ← read this first if you're an AI agent working on this repo
├── docs/
│   ├── FERDY_README.md      ← deep operational reference (config history, incidents, lessons)
│   ├── ADR-001-claim-filing-architecture.md
│   └── browser-diagnosis.md
├── skills/
│   ├── file-claim/          ← BCBS claim filing
│   ├── gps-boleto/          ← Monthly GPS boleto generation
│   └── esocial-dae/         ← Monthly eSocial DAE generation
├── src/
│   ├── server.js            ← Express wrapper (proxy, gateway lifecycle, setup wizard)
│   └── public/              ← Setup wizard frontend
├── TOOLS.md                 ← Workspace-level notes injected into every call (gog CLI, receipt workflow)
├── receipt_sheet_template.py ← Sheets append template (loaded on demand during receipt processing)
├── Dockerfile               ← Production image
├── Dockerfile.base          ← Pre-built base image (Chromium, Python deps) — rebuilt manually via GitHub Actions
├── entrypoint.sh            ← Container startup (Xvfb, Chromium, skills deploy)
└── railway.toml             ← Railway build/deploy config
```

---

## Where to learn more

- **AI agents:** Start with `CLAUDE.md` for project conventions + critical reminders.
- **Operations / incidents / config history:** `docs/FERDY_README.md` is the source of truth for what's broken, what was fixed, and how to safely change Railway config.
- **Claim-filing architecture decisions:** `docs/ADR-001-claim-filing-architecture.md`
- **Browser automation gotchas:** `docs/browser-diagnosis.md`
- **OpenClaw security model + upstream docs:** [docs.openclaw.ai/gateway/security](https://docs.openclaw.ai/gateway/security)

---

## ⚠️ Security note

This template exposes the OpenClaw gateway to the public internet. If you only use Telegram (no need for the web dashboard), you can remove the public Railway endpoint after setup. See the [OpenClaw security docs](https://docs.openclaw.ai/gateway/security) for the full threat model.

## License

MIT (see `LICENSE`).
