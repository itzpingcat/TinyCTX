# TinyCTX — containerized agent runtime
# Python 3.14 (rc) on Debian slim
FROM python:3.14-rc-slim

# --- system deps -----------------------------------------------------------
# playwright needs these; general build sanity
RUN apt-get update && apt-get install -y --no-install-recommends \
        # playwright chromium system deps
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
        # fonts for playwright
        fonts-liberation \
        # build tools (numpy, tiktoken need them at install time)
        gcc g++ \
        # misc
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# --- non-root user ---------------------------------------------------------
RUN groupadd -r tinyctx && useradd -r -g tinyctx -d /app -s /sbin/nologin tinyctx

WORKDIR /app

# --- python deps -----------------------------------------------------------
# Explicitly list real packages only — pyproject.toml has stdlib pseudo-deps
# (socket, ipaddress, urllib) that pip can't install.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        PyYAML aiohttp pyfiglet rich questionary requests mcp numpy \
        tiktoken structlog tenacity ddgs playwright pdfplumber \
        python-docx sympy "antlr4-python3-runtime==4.13.2" jinja2 \
        croniter "discord.py" matrix-nio && \
    playwright install chromium --with-deps 2>/dev/null || \
    playwright install chromium

# --- app source ------------------------------------------------------------
COPY TinyCTX/ ./TinyCTX/
COPY pyproject.toml ./

# Install the package itself (no-deps since we already installed everything above)
RUN pip install --no-cache-dir --no-deps -e .

# --- ownership -------------------------------------------------------------
RUN chown -R tinyctx:tinyctx /app

USER tinyctx

# --- runtime ---------------------------------------------------------------
# Workspace and config.yaml are bind-mounted at runtime (see compose.yaml).
# Nothing sensitive is baked into the image.

EXPOSE 8085

ENTRYPOINT ["python", "TinyCTX/main.py"]
