# SillyTavern → TinyCTX Bridge

Turns SillyTavern into a thin client for a running TinyCTX gateway.
ST's own context pipeline (character cards, lorebooks, chat history) is bypassed entirely —
TinyCTX owns context, memory, and tool execution.

## Architecture

```
SillyTavern UI
    ↕  extension/index.js   (intercepts generation, drives HTTP/SSE natively)
TinyCTX Gateway             (gateway/__main__.py — your running instance)
    ↕
Router → Lane → AgentLoop
```

`bridges/sillytavern/__main__.py` is a no-op stub so this directory doesn't confuse
the bridge loader. There is no server-side component beyond the gateway you already run.

## Setup

### 1. Make sure the gateway is enabled in `config.yaml`

```yaml
gateway:
  enabled: true
  host: 127.0.0.1
  port: 8080
  api_key: "your-secret"
```

### 2. Install the ST extension

Copy the `extension/` folder into SillyTavern's extensions directory:

```
SillyTavern/public/extensions/tinyctx/
  manifest.json
  index.js
  style.css
```

Then in ST: **Extensions → Manage Extensions → TinyCTX → Enable**

### 3. Option A — Global mode (extension settings)

In the TinyCTX extension panel inside ST:
- Check **Enable for all characters**
- Set **Gateway endpoint**, **API key**, **Default session ID**

Every character will now route through TinyCTX.

### 4. Option B — Per-character mode (recommended)

Add a config block to the character's **Description** field:

```
[TINYCTX]
endpoint: http://127.0.0.1:8080
session: my-agent
token: your-secret
[/TINYCTX]
```

Only characters with this block activate TinyCTX mode. Other characters behave normally.
You can have multiple TinyCTX characters each bound to different sessions.

## Slash commands

| Command | Effect |
|---|---|
| `/tinyctx-reset` | Wipe the current TinyCTX session context (`POST /v1/sessions/{id}/reset`) |
| `/tinyctx-status` | Show gateway health and session info |

## Tool event display

When **Show tool call events in chat** is enabled, tool calls appear as italicised
lines in the chat while the agent works:

```
🔧 web_search({"query": "latest news"})
✓ web_search: Result 1: ...
```

Disable in extension settings if you prefer a clean chat view.

## Notes

- ST's chat history is ignored after the first message — TinyCTX manages context.
- Session ID maps directly to a TinyCTX `dm` session lane.
- The gateway's `POST /v1/health` endpoint is public (no auth) and used for the
  connection indicator in the extension panel.
- 429 backpressure from a full lane queue is handled with a single automatic retry.
