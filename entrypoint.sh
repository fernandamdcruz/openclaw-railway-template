#!/bin/bash
# NOTE: no set -e — we want the server to start even if setup steps fail

chown -R openclaw:openclaw /data || echo "[entrypoint] WARNING: chown /data failed"
chmod 700 /data || true

if [ ! -d /data/.linuxbrew ]; then
 cp -a /home/linuxbrew/.linuxbrew /data/.linuxbrew
 fi

 rm -rf /home/linuxbrew/.linuxbrew
 ln -sfn /data/.linuxbrew /home/linuxbrew/.linuxbrew

 # Self-healing: ensure gog is installed (survives volume wipes)
 # IMPORTANT: Run in background with timeout so it never blocks healthcheck
 if ! command -v gog >/dev/null 2>&1; then
 echo "[entrypoint] gog not found, installing in background..."
 (timeout 120 gosu openclaw brew install gogcli 2>&1 && echo "[entrypoint] gogcli installed OK" || echo "[entrypoint] WARNING: gogcli install failed") &
 fi

 # Self-healing: ensure browser profile color fields exist (prevents gateway crash-loop)
 CONFIG_PATH="/data/.openclaw/openclaw.json"
 if [ -f "$CONFIG_PATH" ]; then
 node -e "
 const fs=require('fs'),p='$CONFIG_PATH';
 try{
   const c=JSON.parse(fs.readFileSync(p,'utf8'));
   let patched=false;
   if(c.browser&&c.browser.profiles){
     Object.entries(c.browser.profiles).forEach(([k,v])=>{
       if(!v.color){v.color='#000000';patched=true}
     });
   }
   if(patched){
     fs.writeFileSync(p,JSON.stringify(c,null,2));
     console.log('[entrypoint] Patched config: added missing color to browser profiles');
   }
 }catch(e){console.log('[entrypoint] Config check skipped:',e.message)}
 "
 fi

 # Start Xvfb (virtual display) for Chromium
 if command -v Xvfb >/dev/null 2>&1; then
 Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
 export DISPLAY=:99
 fi

 # Start Chromium with all needed flags, listening on CDP port 9222
 # OpenClaw connects to it via cdpUrl in the openclaw browser profile
 PW_DIR="/home/openclaw/.cache/ms-playwright"
 echo "[entrypoint] Looking for Chromium in $PW_DIR"
 if [ -d "$PW_DIR" ]; then
 echo "[entrypoint] ms-playwright contents: $(ls -d $PW_DIR/*/ 2>/dev/null | head -5)"
 CHROMIUM_BIN=$(find "$PW_DIR" -name "chrome" -type f -path "*/chrome-linux/*" 2>/dev/null | head -1)
 if [ -z "$CHROMIUM_BIN" ]; then
 CHROMIUM_BIN=$(find "$PW_DIR" -name "chrome" -type f 2>/dev/null | head -1)
 fi
 if [ -z "$CHROMIUM_BIN" ]; then
 CHROMIUM_BIN=$(find "$PW_DIR" -name "headless_shell" -type f 2>/dev/null | head -1)
 fi
 fi

 if [ -n "$CHROMIUM_BIN" ]; then
 echo "[entrypoint] Found Chromium at: $CHROMIUM_BIN"
 gosu openclaw "$CHROMIUM_BIN" \
 --remote-debugging-port=9222 \
 --remote-debugging-address=127.0.0.1 \
 --headless=new \
 --no-sandbox \
 --disable-dev-shm-usage \
 --disable-gpu \
 --disable-software-rasterizer \
 --disable-extensions \
 --window-size=1280,720 &
 sleep 2
 echo "[entrypoint] Chromium started on CDP port 9222"
 else
 echo "[entrypoint] No Chromium found, skipping browser start"
 fi

 # Clean stale lock/pid files from previous container (prevents "gateway already running" errors)
 rm -f /data/.openclaw/*.lock /data/.openclaw/*.pid /tmp/.openclaw*.lock 2>/dev/null || true
 echo "[entrypoint] Starting server..."

 exec gosu openclaw node src/server.js
