# TinyCTX — Docker Setup

Runs TinyCTX in a hardened Linux container on your Windows host.

## Quick start

```sh
# 1. Copy and fill in your config
cp example.config.yaml config.yaml

# 2. Make sure your secrets are set in your shell (same as running natively)
#    The onboard wizard does this for you — if you've run it, you're good.
#    If not, set them manually:
#      Windows:  $env:DISCORD_BOT_TOKEN = "your-token"
#      Linux:    export DISCORD_BOT_TOKEN=your-token

# 3. Start
docker compose up -d
```

## How secrets work

TinyCTX reads secrets (like `DISCORD_BOT_TOKEN`) directly from environment
variables. The `compose.yaml` forwards those vars from your host into the
container using bare keys — no `.env` file, no extra config needed. Whatever
your shell has, the container gets.

## What the container can and can't do

| | |
|---|---|
| **Network** | Internet + Tailscale IPs (llama-swap works as-is) |
| **Isolated from** | Other Docker networks on your machine |
| **Reads/writes** | `~/.tinyctx` only (mounted as `/workspace`) |
| **Can't touch** | Anything else on the host filesystem |
| **CPU** | Max 4 cores |
| **RAM** | Max 4 GB |
| **Root fs** | Read-only |
| **Privileges** | Non-root, no-new-privileges, all caps dropped |

## Workspace location

Defaults to `~/.tinyctx`. Override:
```sh
TINYCTX_WORKSPACE=/your/path docker compose up -d
```

## Common commands

```sh
docker compose up -d        # start detached
docker compose logs -f      # follow logs
docker compose down         # stop
docker compose up -d --build  # rebuild after code changes
```

## Notes

**Python 3.14-rc**: If the image is unavailable, change `FROM` in
`Dockerfile` to `python:3.13-slim`.

**Playwright sandbox**: If you see Chromium sandbox errors, add `SYS_ADMIN`
to `cap_add` in `compose.yaml`.
