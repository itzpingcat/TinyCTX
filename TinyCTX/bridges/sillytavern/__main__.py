"""
bridges/sillytavern/__main__.py — SillyTavern thin-client bridge (no-op stub).

This is NOT a true bridge — it has no server-side component.
The actual integration lives entirely in ./extension/, a SillyTavern extension
that talks directly to the TinyCTX gateway API.

Architecture:
  SillyTavern UI
    ↕  extension/index.js  (generation interceptor + session UI)
  TinyCTX Gateway          (gateway/__main__.py — your running instance)
    ↕
  Router → Lane → AgentLoop

Extension features:
  - Manages one character ("TinyCTX Agent") — created automatically on first load
  - Character card fields sync to gateway workspace files:
      description  → AGENTS.md
      personality  → SOUL.md
      scenario     → MEMORY.md
  - Session list panel replaces ST's chat list for the managed character
  - Sessions pulled live from GET /v1/health, selectable by click
  - Generation intercepted via manifest generate_interceptor → window.tinyCTXIntercept
  - Only raw user text forwarded; TinyCTX owns all context and history
  - Gateway config (endpoint, api_key) stored in ST extensionSettings — no card hacks
  - Slash commands: /tinyctx-reset, /tinyctx-status, /tinyctx-new-session

Install: copy extension/ into SillyTavern/public/scripts/extensions/third-party/tinyctx/
"""


async def run(gateway) -> None:  # noqa: ARG001
    """No-op. This bridge has no server-side component."""
    return
