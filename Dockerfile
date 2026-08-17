# TinyCTX — containerized agent runtime
FROM python:3.14-rc-slim

# Fixed UID/GID for the tinyctx user — MUST match sandbox/Dockerfile exactly.
# Both containers bind-mount the same host workspace to /home/tinyctx, so if
# these drift apart (or drift from whatever `useradd -r` felt like assigning
# on a given rebuild), one container silently loses write access to files the
# other created. useradd -r with no explicit --uid picks "the next free
# system UID," which depends on the base image's snapshot — it is NOT stable
# across rebuilds, especially since this image tracks the moving
# python:3.14-rc-slim tag. Pinning it here is what makes it stable.
ARG TINYCTX_UID=10000
ARG TINYCTX_GID=10000

# --- env -------------------------------------------------------------------
ENV HOME=/home/tinyctx

# Camoufox resolves its cache dir via platformdirs.user_cache_dir("camoufox"),
# which honors XDG_CACHE_HOME before falling back to $HOME/.cache. Since
# /home/tinyctx is a bind-mount target at runtime (compose.yaml mounts the
# host workspace over it), anything camoufox fetches under $HOME/.cache during
# the build would be shadowed once the container starts. Point XDG_CACHE_HOME
# at a path outside any mounted volume so the fetched browser survives.
ENV XDG_CACHE_HOME=/opt/camoufox-cache

# --- system deps -----------------------------------------------------------
# xvfb backs Camoufox's headless="virtual" mode: a real headful Firefox on a
# virtual display, which bot checks accept where true-headless is detected.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
        libgtk-3-0 libx11-xcb1 libxtst6 libxt6 libdbus-glib-1-2 \
        fonts-liberation \
        gcc g++ \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# --- create non-root user --------------------------------------------------
RUN groupadd -g ${TINYCTX_GID} tinyctx && \
    useradd -u ${TINYCTX_UID} -g tinyctx -d /home/tinyctx -m -s /sbin/nologin tinyctx

WORKDIR /app

# --- camoufox (pinned first so it never re-runs when other deps change) ----
RUN mkdir -p /opt/camoufox-cache
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "camoufox[geoip]" pyvirtualdisplay && camoufox fetch

# --- app source ------------------------------------------------------------
COPY TinyCTX/ ./TinyCTX/
COPY pyproject.toml ./

RUN pip install --no-cache-dir -e ".[agent]"

# --- config dir (users.db lives here, outside the workspace mount) --------
RUN mkdir -p /etc/tinyctx && chown tinyctx:tinyctx /etc/tinyctx

# --- permissions -----------------------------------------------------------
RUN chown -R tinyctx:tinyctx /home/tinyctx /opt/camoufox-cache

USER tinyctx

# --- runtime ---------------------------------------------------------------
EXPOSE 8085
ENTRYPOINT ["python", "TinyCTX/main.py"]
