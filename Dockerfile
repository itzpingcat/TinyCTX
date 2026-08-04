# TinyCTX — containerized agent runtime
FROM python:3.14-rc-slim

# --- env -------------------------------------------------------------------
# Camoufox installs its browser under $HOME/.cache/camoufox
ENV HOME=/home/tinyctx

# --- system deps -----------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
        fonts-liberation \
        gcc g++ \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# --- create non-root user --------------------------------------------------
RUN groupadd -r tinyctx && useradd -r -g tinyctx -d /home/tinyctx -m -s /sbin/nologin tinyctx

WORKDIR /app

# --- camoufox (pinned first so it never re-runs when other deps change) ----
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "camoufox[geoip]" && camoufox fetch

# --- app source ------------------------------------------------------------
COPY TinyCTX/ ./TinyCTX/
COPY pyproject.toml ./

RUN pip install --no-cache-dir -e .

# --- config dir (users.db lives here, outside the workspace mount) --------
RUN mkdir -p /etc/tinyctx && chown tinyctx:tinyctx /etc/tinyctx

# --- permissions -----------------------------------------------------------
RUN chown -R tinyctx:tinyctx /home/tinyctx

USER tinyctx

# --- runtime ---------------------------------------------------------------
EXPOSE 8085
ENTRYPOINT ["python", "TinyCTX/main.py"]
