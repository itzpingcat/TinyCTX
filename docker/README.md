# TinyCTX — Docker Setup

Runs TinyCTX in a hardened Linux container on your Windows host.
The agent is sandboxed: it can't see your host filesystem (except the
workspace it's explicitly given), can't escalate privileges, and can't
eat all your RAM/CPU.

## Usage

```powershell
# From repo root
cd docker

# Build (takes a few minutes first time — playwright downloads chromium)
docker compose build

# Start detached
docker compose up -d

# Logs
docker compose logs -f

# Stop
docker compose down
```

## What the container can and can't do

| | |
|---|---|
| **Network** | Internet + Tailscale IPs (llama-swap at 100.64.72.3:5000 works as-is) |
| **Isolated from** | Other Docker networks on your machine (Matrix stack etc.) |
| **Reads/writes** | `~/.tinyctx` only (mounted as `/workspace`) |
| **Can't touch** | Anything else on the host filesystem |
| **CPU** | Max 4 cores |
| **RAM** | Max 4 GB |
| **Root fs** | Read-only — container code can't be tampered with at runtime |
| **Privileges** | Non-root user, no-new-privileges, all Linux caps dropped |

## Gateway port

`127.0.0.1:8080` on the host. Your bridges connect the same as before.

## Code changes made to TinyCTX

Two env var overrides were added to `TinyCTX/config/__main__.py`:

- `TINYCTX_WORKSPACE_OVERRIDE` — redirects the workspace path so the
  Windows path in config.yaml doesn't break inside the container.
- `TINYCTX_GATEWAY_HOST` — forces the gateway to bind `0.0.0.0` so
  Docker port forwarding can reach it (config.yaml has `127.0.0.1`).

## Notes

**Python 3.14-rc**: If `python:3.14-rc-slim` is unavailable, change the
`FROM` line in Dockerfile to `python:3.13-slim` — should work fine.

**Tailscale**: llama-swap's Tailscale IP is reachable from inside the
container because Docker bridge traffic routes through the Windows host.
Requires Tailscale to be running on Windows.

**Playwright sandbox**: If you see Chromium sandbox errors, add
`SYS_ADMIN` to `cap_add` in compose.yaml and set env var
`PLAYWRIGHT_CHROMIUM_SANDBOX=0`.

**Rebuild after code changes**:
```powershell
docker compose build && docker compose up -d
```
