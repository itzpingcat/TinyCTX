# sandbox/Dockerfile
#
# Shell execution sandbox for TinyCTX.
# Loaded with tools so the agent never hits a dead end.
# Runs as non-root with a read-only root FS (workspace is a bind mount).

FROM python:3.12-slim

# ── system packages ────────────────────────────────────────────────────────
# Core utils, VCS, media, build tools, language runtimes
RUN apt-get update && apt-get install -y --no-install-recommends \
    \
    # Firewall
    iptables \
    \
    # Shell / core utils
    bash curl wget ca-certificates \
    coreutils findutils grep sed gawk diffutils patch \
    less tree jq unzip zip tar gzip bzip2 xz-utils \
    file man-db \
    \
    # VCS
    git git-lfs \
    \
    # Build essentials (needed by many pip packages, node-gyp, etc.)
    gcc g++ make cmake pkg-config \
    libssl-dev libffi-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev liblzma-dev \
    \
    # Network tools the agent might legitimately need
    dnsutils iputils-ping netcat-openbsd openssh-client \
    \
    # Media processing
    ffmpeg imagemagick \
    \
    # Document / data tools
    sqlite3 \
    poppler-utils \
    \
    # Search
    ripgrep \
    \
    # Text processing
    pandoc \
    \
    # Node.js (LTS via nodesource)
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    \
    && rm -rf /var/lib/apt/lists/*

# ── npm globals ────────────────────────────────────────────────────────────
RUN npm install -g \
    typescript \
    ts-node \
    prettier \
    eslint \
    nodemon \
    yarn \
    pnpm

# ── python packages ────────────────────────────────────────────────────────
# aiohttp for the service itself, plus a wide battery of commonly needed libs
RUN pip install --no-cache-dir \
    \
    # Service
    aiohttp \
    \
    # HTTP / web
    requests httpx beautifulsoup4 lxml html5lib \
    \
    # Data science / numerics
    numpy pandas scipy matplotlib seaborn \
    scikit-learn statsmodels \
    \
    # Data formats
    openpyxl xlrd xlwt \
    PyYAML toml \
    ujson orjson \
    \
    # Document processing
    pdfplumber pypdf2 python-docx python-pptx pillow \
    \
    # Templating / text
    jinja2 markdown pygments \
    \
    # Databases
    sqlalchemy psycopg2-binary pymysql \
    \
    # Dev / code tools
    black isort mypy pylint pytest \
    gitpython \
    \
    # Crypto / encoding
    cryptography pyotp \
    \
    # CLI / system
    click rich typer tqdm \
    psutil \
    \
    # Date / time
    python-dateutil arrow \
    \
    # Async
    anyio trio

# ── non-root user ──────────────────────────────────────────────────────────
RUN groupadd -r tinyctx && \
    useradd -r -g tinyctx -d /home/tinyctx -m -s /bin/bash tinyctx

# Workspace mount point
RUN mkdir -p /home/tinyctx && chown tinyctx:tinyctx /home/tinyctx

WORKDIR /app

# ── service code ───────────────────────────────────────────────────────────
COPY sandbox/__main__.py ./sandbox/__main__.py
COPY sandbox/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN chown -R tinyctx:tinyctx /app

# Stay as root so entrypoint.sh can apply iptables rules,
# then it drops to tinyctx via su-exec.
EXPOSE 8700

ENTRYPOINT ["/entrypoint.sh"]
