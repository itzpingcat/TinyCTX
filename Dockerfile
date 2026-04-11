# TinyCTX — containerized agent runtime
FROM python:3.14-rc-slim

# --- env -------------------------------------------------------------------
# Force Playwright to install browsers in a shared path
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

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
RUN groupadd -r tinyctx && useradd -r -g tinyctx -d /app -s /sbin/nologin tinyctx

WORKDIR /app

# --- python deps -----------------------------------------------------------
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        PyYAML aiohttp pyfiglet rich questionary requests mcp numpy \
        tiktoken structlog tenacity ddgs playwright pdfplumber \
        python-docx sympy "antlr4-python3-runtime==4.13.2" jinja2 \
        croniter "discord.py" matrix-nio

# --- install playwright browsers (GLOBAL PATH) -----------------------------
RUN playwright install chromium

# --- app source ------------------------------------------------------------
COPY TinyCTX/ ./TinyCTX/
COPY pyproject.toml ./

RUN pip install --no-cache-dir --no-deps -e .

# --- permissions -----------------------------------------------------------
RUN chown -R tinyctx:tinyctx /app /ms-playwright

USER tinyctx

# --- runtime ---------------------------------------------------------------
EXPOSE 8085
ENTRYPOINT ["python", "TinyCTX/main.py"]