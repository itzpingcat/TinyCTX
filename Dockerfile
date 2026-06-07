# TinyCTX — containerized agent runtime
FROM python:3.14-rc-slim

# --- env -------------------------------------------------------------------
# Force Playwright to install browsers in a shared path
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/tinyctx

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

# --- playwright (pinned first so it never re-runs when other deps change) --
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/ms-playwright \
    pip install playwright && playwright install chromium

# --- python deps -----------------------------------------------------------
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install \
        PyYAML aiohttp pyfiglet rich questionary requests mcp numpy \
        tiktoken structlog tenacity ddgs pdfplumber ladybug \
        python-docx Pillow sympy "antlr4-python3-runtime==4.13.2" jinja2 \
        platformdirs croniter "discord.py" matrix-nio onnxruntime

# --- app source ------------------------------------------------------------
COPY TinyCTX/ ./TinyCTX/
COPY pyproject.toml ./

RUN pip install --no-cache-dir --no-deps -e .

# --- config dir (users.db lives here, outside the workspace mount) --------
RUN mkdir -p /etc/tinyctx && chown tinyctx:tinyctx /etc/tinyctx

# --- permissions -----------------------------------------------------------
RUN chown -R tinyctx:tinyctx /home/tinyctx /ms-playwright

USER tinyctx

# --- runtime ---------------------------------------------------------------
EXPOSE 8085
ENTRYPOINT ["python", "TinyCTX/main.py"]
