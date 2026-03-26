FROM node:22-bookworm

ARG OPENCLAW_INSTALL_BROWSER

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
 ca-certificates \
 curl \
 git \
 gosu \
 procps \
 python3 \
 python3-pip \
 build-essential \
 zip \
 xvfb \
 && rm -rf /var/lib/apt/lists/*

 # Install Python Playwright for claim_filer.py
 RUN pip install --break-system-packages playwright

 RUN npm install -g openclaw@2026.3.13 clawhub@latest

 WORKDIR /app

 COPY package.json pnpm-lock.yaml ./
 RUN corepack enable && pnpm install --frozen-lockfile --prod

 COPY src ./src
 COPY --chmod=755 entrypoint.sh ./entrypoint.sh

 # Install Playwright Chromium with system dependencies if OPENCLAW_INSTALL_BROWSER=1
 RUN if [ "$OPENCLAW_INSTALL_BROWSER" = "1" ]; then \
 mkdir -p /home/node/.cache/ms-playwright && \
 PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright \
 node /usr/local/lib/node_modules/openclaw/node_modules/playwright-core/cli.js install --with-deps chromium && \
 chmod -R 777 /home/node/.cache/ms-playwright; \
 fi

 RUN useradd -m -s /bin/bash openclaw \
 && chown -R openclaw:openclaw /app \
 && mkdir -p /data && chown openclaw:openclaw /data \
 && mkdir -p /home/linuxbrew/.linuxbrew && chown -R openclaw:openclaw /home/linuxbrew \
 && if [ -d /home/node/.cache/ms-playwright ]; then \
 mkdir -p /home/openclaw/.cache && \
 cp -a /home/node/.cache/ms-playwright /home/openclaw/.cache/ms-playwright && \
 chown -R openclaw:openclaw /home/openclaw/.cache; \
 fi

 USER openclaw
 RUN NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

 # Install gog CLI for Gmail/Sheets/Drive access (formula name is gogcli)
 RUN /home/linuxbrew/.linuxbrew/bin/brew install gogcli

 ENV PATH="/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:${PATH}"
 ENV HOMEBREW_PREFIX="/home/linuxbrew/.linuxbrew"
 ENV HOMEBREW_CELLAR="/home/linuxbrew/.linuxbrew/Cellar"
 ENV HOMEBREW_REPOSITORY="/home/linuxbrew/.linuxbrew/Homebrew"
 ENV PLAYWRIGHT_BROWSERS_PATH="/home/openclaw/.cache/ms-playwright"

 ENV PORT=8080
 ENV OPENCLAW_ENTRY=/usr/local/lib/node_modules/openclaw/dist/entry.js
 EXPOSE 8080

 HEALTHCHECK --interval=5s --timeout=10s --start-period=120s --retries=10 \
 CMD curl -f http://localhost:8080/setup/healthz || exit 1

 # COPY skills LAST so changes to skill files don't invalidate the expensive
 # Chromium/Homebrew/gogcli layers above. This keeps rebuilds under 30s.
 COPY skills ./skills

 USER root
 ENTRYPOINT ["./entrypoint.sh"]
