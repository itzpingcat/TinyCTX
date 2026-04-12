# PLAN: Agent File Delivery via `present()` Tool

## Core Idea

The `present()` tool delivers files by acting directly — not by returning a
sentinel string for `agent.py` to parse.

When called, `present()` has a reference to the agent. It validates the file
paths, then calls the router's event handler directly with an
`AgentOutboundFiles` event. The tool returns a plain success string to the
agent:

```
Successfully sent files: report.pdf, summary.txt
```

The agent sees a normal tool result. No sentinel detection. No regex. No
structured payload hidden in a return string. The router sees a typed event
it knows how to dispatch.

---

## What This Does NOT Do

- No sentinel strings in `contracts.py`.
- No detection logic in `agent.py`.
- No new fields on `ToolResult`.
- No new fields on `AgentToolResult`.
- No SSE changes in the gateway.
- No per-bridge new code paths for file handling — bridges get a first-class
  typed event they dispatch like any other.

---

## Scope of Changes

### 1. `contracts.py` — one new event type

```python
@dataclass(frozen=True)
class AgentOutboundFiles(_AgentEventBase):
    """
    Emitted directly by the present() tool when the agent wants to deliver
    files to the user. Bridges send each path as a file attachment.
    """
    paths: tuple[str, ...]
```

Add `AgentOutboundFiles` to the `AgentEvent` union.

This is the only new dataclass. It is a first-class event — not a hack bolted
onto an existing one.

---

### 2. `modules/present/` (new module)

A single always-on tool: `present(media)`.

**`modules/present/__init__.py`**
```python
EXTENSION_META = {
    "name": "present",
    "description": "Lets the agent deliver files to the user.",
    "module_type": "per_lane",
    "default_config": {},
}
```

**`modules/present/__main__.py`**

```python
def register(agent) -> None:
    from pathlib import Path
    import asyncio

    workspace = Path(agent.config.workspace.path).expanduser().resolve()

    def present(media: list[str]) -> str:
        """Deliver files to the user.

        This is the ONLY way to deliver files to the user. Pass workspace-
        relative or absolute paths in `media`. Do NOT use read_file to send
        files — that only reads content for your own analysis.

        Args:
            media: List of file paths (workspace-relative or absolute) to
                   deliver to the user.
        """
        from TinyCTX.contracts import AgentOutboundFiles

        validated: list[str] = []
        for p in media:
            try:
                resolved = (workspace / p).resolve()
                resolved.relative_to(workspace)          # path traversal guard
            except ValueError:
                return f"Error: {p} is outside the workspace"
            if not resolved.is_file():
                return f"Error: {p} not found"
            validated.append(str(resolved))

        # Build the event and fire it directly through the router.
        # agent.gateway is the Router instance set by Lane.__post_init__.
        event = AgentOutboundFiles(
            paths=tuple(validated),
            tail_node_id=agent.tail_node_id,
            lane_node_id=agent.lane_node_id,
            trace_id="present",        # not tied to a specific turn
            reply_to_message_id="",
        )
        asyncio.get_event_loop().create_task(agent.gateway._dispatch_event(event))

        names = ", ".join(Path(p).name for p in validated)
        return f"Successfully sent files: {names}"

    agent.tool_handler.register_tool(present, always_on=True)
```

The tool fires the event as a detached asyncio task and immediately returns
the success string. The agent logs a normal tool result. Bridges receive
`AgentOutboundFiles` through the existing `_dispatch_event` path.

---

### 3. `bridges/discord/__main__.py`

Add `AgentOutboundFiles` to the imports and handle it in `handle_event`:

```python
elif isinstance(event, AgentOutboundFiles):
    for path in event.paths:
        try:
            await self._channel_for(event).send(file=discord.File(path))
        except Exception:
            logger.warning("Discord: failed to upload file %s", path)
```

`_channel_for(event)` looks up the channel via the accumulator or a
cursor→channel map that `_handle_turn` already maintains. Discord's 8 MB
per-file limit applies; validate size in a follow-up if needed.

> **Note:** `handle_event` needs the channel object to send files. The Discord
> bridge already stores the channel in `_handle_turn` via the accumulator. The
> simplest approach is to add `_channels: dict[str, discord.abc.Messageable]`
> (keyed by `lane_node_id`), populated at the top of `_handle_turn` and
> cleared in `finally`. `AgentOutboundFiles` arrives during the same turn, so
> the channel is always present.

---

### 4. `bridges/cli/__main__.py`

Handle `AgentOutboundFiles` in the SSE event loop. The gateway will serialise
it as a new SSE event type `outbound_files` (see §5):

```python
elif event_type == "outbound_files":
    paths = data.get("paths", [])
    self._stop_live()
    c = self._theme.c
    self._console.print(f"[{c('tool_ok')}]  ↓  files presented:[/{c('tool_ok')}]")
    for p in paths:
        self._console.print(f"     {p}", markup=False, style="bright_black")
```

---

### 5. `gateway/__main__.py` — serialise `AgentOutboundFiles`

Add to `_event_to_dict`:

```python
if isinstance(event, AgentOutboundFiles):
    return {
        "type":  "outbound_files",
        "paths": list(event.paths),
    }
```

Clients receiving `outbound_files` fetch each file from the existing
`GET /v1/workspace/files/{path}` endpoint.

---

### 6. `bridges/matrix/__main__.py` (follow-up)

Handle `AgentOutboundFiles`: upload each path via the Matrix media upload API
after the text reply. Lower priority.

---

## Files Changed Summary

| File | Change |
|------|--------|
| `contracts.py` | Add `AgentOutboundFiles` event; add to `AgentEvent` union |
| `modules/present/__init__.py` | New file |
| `modules/present/__main__.py` | New file — registers always-on `present()` tool; fires event directly |
| `gateway/__main__.py` | Serialise `AgentOutboundFiles` as `outbound_files` SSE event |
| `bridges/cli/__main__.py` | Handle `outbound_files` SSE event — print paths |
| `bridges/discord/__main__.py` | Handle `AgentOutboundFiles` — upload files to channel |

---

## Order of Implementation

1. `contracts.py` — add `AgentOutboundFiles`
2. `modules/present/` — self-contained, testable in isolation
3. `gateway/__main__.py` — serialise the new event
4. `bridges/cli/__main__.py` — print paths from SSE
5. `bridges/discord/__main__.py` — upload files; add `_channels` map to accumulator

---

## Comparison with Previous Plans

| | Sentinel plan | This plan |
|---|---|---|
| New event types | None (hacked onto `AgentToolResult`) | `AgentOutboundFiles` (clean) |
| Sentinel strings | `PRESENT_TOOL:` | None |
| Detection logic in `agent.py` | Yes — string prefix check | None |
| New fields on `ToolResult` / `AgentToolResult` | `media` on both | None |
| Tool return value | Structured JSON payload | Plain human-readable string |
| Agent sees | Opaque sentinel it must decode | Normal success message |
| Routing path | sentinel → agent.py → `AgentToolResult.media` → bridges | tool → `_dispatch_event` → bridges |
| New SSE event type | None (abused `tool_result`) | `outbound_files` |
| Files changed | 7 | 6 |

The key difference: delivery happens at the tool call site, not in `agent.py`.
The agent receives a normal string result. Bridges receive a typed event.
Nothing in the middle needs to know about files.
