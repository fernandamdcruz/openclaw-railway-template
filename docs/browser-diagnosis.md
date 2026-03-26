# FerdyBot Browser Tool Diagnosis

## The Problem

FerdyBot's browser tool (Playwright/Chromium running inside the Railway Docker container) reports as "ready" but immediately times out on the first actual use. This pattern is 100% consistent — it happens every time, even after restarts.

**From the deploy logs (Mar 23):**

- `00:16:57` — `[browser/service] Browser control service ready (profiles=4)`
- `00:17:12` — `[tools] browser failed: timed out. Restart the OpenClaw gateway... Do NOT retry the browser tool — it will keep failing.`

The service finds the browser installation and profile configs (reports "ready"), but when it actually tries to launch Chromium to do something, it hangs and times out after ~15 seconds.

---

## Root Causes (ranked by likelihood)

### 1. `/dev/shm` too small (MOST LIKELY)

Docker containers default to **64 MB of shared memory** (`/dev/shm`). Chromium requires **significantly more** — typically 1–2 GB. When shared memory is insufficient, Chromium launches but immediately crashes or freezes when trying to render pages.

Railway does **not** expose `--shm-size` in `railway.toml` or through the UI. There is no workaround in the current Dockerfile.

This perfectly explains the symptom: service says "ready" (found Playwright binaries + profile configs) → first real browser call times out (Chromium crashes silently due to insufficient shared memory).

### 2. Xvfb not started

The Dockerfile installs `xvfb` (X virtual framebuffer), which suggests the browser needs a display server. However, the `entrypoint.sh` does **not** start Xvfb and does **not** set the `DISPLAY` environment variable.

If Playwright is configured to run Chromium in non-headless mode (which some browser profiles may require), it would hang trying to connect to an X11 display that doesn't exist.

**entrypoint.sh currently:**
```bash
#!/bin/bash
set -e
chown -R openclaw:openclaw /data
chmod 700 /data
# ... linuxbrew setup ...
exec gosu openclaw node src/server.js
```

**Missing:** `Xvfb :99 -screen 0 1920x1080x24 &` and `export DISPLAY=:99`

### 3. Missing `--no-sandbox` flag

Chromium in Docker containers needs the `--no-sandbox` flag because Docker doesn't provide the kernel user namespaces that Chromium's sandbox requires. If OpenClaw's browser launcher doesn't pass this flag, Chromium will fail to start.

This is controlled inside the `openclaw` npm package (v2026.3.13), not in the template repo. We can't verify without checking the package source.

### 4. Too many browser profiles (4 instead of 2)

The service initializes **4 browser profiles**, but only **2 are actually used**:
- `openclaw` — for BCBS
- `browserbase` — for gov.br portals (GPS, eSocial)

The other 2 profiles are unused overhead. If each profile pre-launches a Chromium instance, that's 4× the memory and shared memory pressure — making the `/dev/shm` problem even worse.

### 5. Healthcheck timeout barely sufficient

- Healthcheck timeout: **300 seconds** (5 minutes)
- First deploy attempt: **FAILED** at 4:53 (just 7 seconds under the limit)
- Browser service takes ~7 minutes to fully initialize
- The successful deploy only passed because persisted volume data shortened startup

Any slightly slower startup races the healthcheck and fails.

---

## Configuration Snapshot

| Setting | Value |
|---------|-------|
| Railway Plan Limits | 8 vCPU, 8 GB RAM |
| OPENCLAW_INSTALL_BROWSER | `1` |
| Healthcheck Path | `/setup/healthz` |
| Healthcheck Timeout | 300s |
| Restart Policy | On Failure (max 10) |
| Start Command | `npm run start` → `entrypoint.sh` → `node src/server.js` |
| Base Image | `node:22-bookworm` |
| Browser | Playwright Chromium (installed at build time) |
| Profiles Active | 4 (only 2 needed) |
| Xvfb | Installed but NOT started |
| Fork Status | 3 commits behind upstream |

---

## Recommended Fixes (in priority order)

1. **Add Xvfb startup to entrypoint.sh** — Start `Xvfb :99` and set `DISPLAY=:99` before launching the server. This is the easiest fix to try first.

2. **Reduce profiles from 4 to 2** — Remove the unused profiles to cut memory usage and startup time in half. This alone might fix the healthcheck timeout issue.

3. **Investigate `/dev/shm`** — Check if Railway supports `docker run --shm-size` or if there's a workaround (e.g., mounting a tmpfs at `/dev/shm` in the entrypoint). If not, this may be a Railway platform limitation.

4. **Ensure `--no-sandbox` is passed** — Check if the `openclaw` package passes this flag to Chromium. If not, file an issue or find a way to configure it.

5. **Increase healthcheck timeout** — Bump from 300s to 600s in `railway.toml` to give the browser service more time to initialize.

6. **Consider Browserbase for ALL browser tasks** — Instead of running local Chromium in Docker (which has all these compatibility issues), use Browserbase's cloud browser service for everything. This would eliminate the Docker+Chromium problems entirely.

---

## Quick Reference: The Error You Saw

> "The browser control service is currently unavailable. This usually means: The browser service is still initializing... or The browser could not be started due to missing dependencies."

This error maps directly to root causes #1–3 above. The browser binaries are there, but Chromium can't actually run properly inside the container.
