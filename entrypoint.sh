#!/bin/bash
set -e

chown -R openclaw:openclaw /data
chmod 700 /data

if [ ! -d /data/.linuxbrew ]; then
  cp -a /home/linuxbrew/.linuxbrew /data/.linuxbrew
  fi

  rm -rf /home/linuxbrew/.linuxbrew
  ln -sfn /data/.linuxbrew /home/linuxbrew/.linuxbrew

  # Start Xvfb (virtual display) for Chromium
  if command -v Xvfb >/dev/null 2>&1; then
    Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
      export DISPLAY=:99
      fi

      # Start Chromium with all needed flags, listening on CDP port 9222
      # OpenClaw connects to it via cdpUrl in the openclaw browser profile
      CHROMIUM_BIN=$(ls /home/openclaw/.cache/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null | head -1)
      if [ -n "$CHROMIUM_BIN" ]; then
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

                                                  exec gosu openclaw node src/server.js
                                                  
