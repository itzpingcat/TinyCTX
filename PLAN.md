# PLAN: Agent File Delivery via `message()` Tool

## Core Idea

File delivery is a property of a message, not a separate event.

The original plan threaded a `PRESENT_FILES:` sentinel through `agent.py`,
added a new `AgentFileAttachment` event type to `contracts.py`, and required
every bridge to handle a new event kind. This is unnecessary complexity.

Nanobot's `MessageTool` shows the better approach: file paths are just a `media`
parameter on the existing outbound message. The agent calls
`message(content="Here are your files", media=["report.pdf"])` and every
bridge already knows how to send it — because it already handles outbound text.

This plan follows that model exactly.

---

## What This Does NOT Do

- No new event types in `contracts.py` (no `AgentFileAttachment`).
- No sentinel strings, no detection logic in `agent.py`.
- No new fields on `ToolResult`.
- No new module directory (`modules/present/` is gone).
- No new SSE event type in the gateway.
- No per-bridge new code paths — file handling lives in the same outbound
  handler as text, just with extra paths attached.
- No config changes needed. No `config.yaml` keys added.

---

## Scope of Changes

### 1. `contracts.py` — one small addition

Add `media` to `ToolResult` so `agent.py` can carry file paths alongside the
tool output string without a sentinel hack:

```python
@dataclass(frozen=True)
class ToolResult:
    call_id:    str
    tool_name:  str
    output:     str
    is_error:   bool        = False
    is_image:   bool        = False
    image_mime: str | None  = None
    image_b64:  str | None  = None
    media:      tuple[str, ...] = ()   # <-- new: workspace-validated file paths
```

The default is `()` so all existing `ToolResult(...)` construction sites are
unchanged.

---

### 2. `modules/message/` (new module, replaces `modules/present/`)

A single always-on tool: `message(content, media=None)`.

**`modules/message/__init__.py`**
```python
EXTENSION_META = {
    "name": "message",
    "description": "Lets the agent send messages and files to the user.",
    "module_type": "per_lane",
    "default_config": {},
}
```

**`modules/message/__main__.py`**

```python
def register(agent) -> None:
    from pathlib import Path
    import json

    workspace = str(agent.config.workspace.path)

    def message(content: str, media: list[str] | None = None) -> str:
        """Send a message to the user, optionally with file attachments.

        This is the ONLY way to deliver files to the user. Pass workspace-
        relative or absolute paths in `media`. Do NOT use read_file to send
        files — that only reads content for your own analysis.

        Args:
            content: The message text to send.
            media:   Optional list of file paths (workspace-relative or
                     absolute) to attach and deliver to the user.
        """
        root = Path(workspace).expanduser().resolve()
        validated: list[str] = []

        for p in (media or []):
            try:
                resolved = (root / p).resolve()
                resolved.relative_to(root)          # path traversal guard
            except ValueError:
                return f"Error: {p} is outside the workspace"
            if not resolved.is_file():
                return f"Error: {p} not found"
            validated.append(str(resolved))

        # Return a structured result that agent.py will detect.
        from TinyCTX.contracts import MESSAGE_TOOL_PREFIX
        return MESSAGE_TOOL_PREFIX + json.dumps({
            "content": content,
            "media": validated,
        })

    agent.tool_handler.register_tool(message, always_on=True)
```

The tool is always-on for the same reason `present()` was: the agent must be
able to call it at any point without searching for it first.

---

### 3. `contracts.py` — sentinel constant

```python
# Returned by the message() tool.
# Format: MESSAGE_TOOL_PREFIX + JSON {"content": "...", "media": [...]}
# agent._execute_tool detects this and populates ToolResult.media.
MESSAGE_TOOL_PREFIX = "MESSAGE_TOOL:"
```

This lives alongside the existing `IMAGE_BLOCK_PREFIX`. Same pattern, same
detection logic in `agent.py`.

---

### 4. `agent.py` — detect sentinel, populate `ToolResult.media`

In `_execute_tool`, after the existing `IMAGE_BLOCK_PREFIX` branch, add:

```python
if not is_error and raw_output.startswith(MESSAGE_TOOL_PREFIX):
    import json as _json
    payload = _json.loads(raw_output[len(MESSAGE_TOOL_PREFIX):])
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        output=payload["content"],      # text shown in the tool result block
        is_error=False,
        media=tuple(payload.get("media", [])),
    )
```

Then in `run()`, after yielding `AgentToolResult`, emit a synthetic outbound
message if `result.media` is non-empty:

```python
if result.media:
    yield AgentTextFinal(
        text=result.output,
        tail_node_id=ev["tail_node_id"],
        lane_node_id=ev.get("lane_node_id"),
        trace_id=ev["trace_id"],
        reply_to_message_id=ev["reply_to_message_id"],
    )
    # Signal bridges to flush the outbound message with attachments.
    # We reuse AgentTextFinal so bridges need no new handling.
    # The router knows to attach result.media to the outbound flush.
```

> **Note:** The exact plumbing between `agent.py` and the router for the
> `media` payload depends on whether the bridge pulls files off the
> `AgentToolResult` event directly or off a separate signal. The two
> approaches are laid out in the bridge sections below. Pick whichever
> is cleaner after reading `router.py`.

**Option A — carry `media` on `AgentToolResult`** (simpler)

Bridges inspect `event.media` on `AgentToolResult`. When non-empty they queue
the files and upload them right after the current text reply is sent.

Add `media: tuple[str, ...] = ()` to `AgentToolResult` in `contracts.py`
(same default pattern as `ToolResult`). Populate it from `result.media` when
yielding the event:

```python
yield AgentToolResult(
    ...,
    media=result.media,
)
```

**Option B — separate `AgentOutboundMessage` event** (more explicit)

Add one new frozen dataclass to `contracts.py`:

```python
@dataclass(frozen=True)
class AgentOutboundMessage(_AgentEventBase):
    """
    Emitted when the agent calls message() with content and optional files.
    Bridges send the content as text and deliver any media paths as attachments.
    """
    content: str
    media:   tuple[str, ...] = ()
```

This is more explicit but adds one new event type. Option A is preferred
unless you find bridges need to distinguish tool-result text from user-facing
text, in which case Option B is cleaner.

The rest of this plan assumes **Option A** for simplicity.

---

### 5. `gateway/__main__.py` — include `media` in `tool_result` SSE event

`_event_to_dict` already serialises `AgentToolResult`. Add `media`:

```python
if isinstance(event, AgentToolResult):
    return {
        "type":      "tool_result",
        "tool_name": event.tool_name,
        "call_id":   event.call_id,
        "output":    event.output,
        "is_error":  event.is_error,
        "media":     list(event.media),   # <-- new, empty list if none
    }
```

No new SSE event type. Clients that receive a `tool_result` with a non-empty
`media` array fetch each file from the existing
`GET /v1/workspace/files/{path}` endpoint. Update the gateway module docstring
to document the new `media` field.

---

### 6. `bridges/discord/__main__.py` — upload files from `AgentToolResult`

`handle_event` already handles `AgentToolResult`. Extend it:

```python
elif isinstance(event, AgentToolResult):
    logger.debug(
        "Discord: tool result %s (%s) for cursor %s",
        event.tool_name, "error" if event.is_error else "ok", node_id,
    )
    if event.media and acc is not None:
        acc.queue_media(list(event.media))
```

Add `queue_media` and `pending_media` to `_ReplyAccumulator`:

```python
class _ReplyAccumulator:
    def __init__(self, channel, max_len):
        ...
        self._pending_media: list[str] = []

    def queue_media(self, paths: list[str]) -> None:
        self._pending_media.extend(paths)

    async def wait_and_send(self, timeout=None) -> None:
        # ... existing text send logic ...
        for path in self._pending_media:
            try:
                await self._channel.send(file=discord.File(path))
            except Exception:
                logger.warning("Discord: failed to upload file %s", path)
```

Files are uploaded after the text reply, in the same `wait_and_send` call.
Discord's 8 MB per-file limit applies; out-of-scope for this plan (validate
size if needed in a follow-up).

---

### 7. `bridges/cli/__main__.py`

The CLI bridge talks to the gateway over SSE. When it receives a `tool_result`
event with a non-empty `media` array, print the paths:

```python
elif event_type == "tool_result":
    media = data.get("media", [])
    if media:
        self._stop_live()
        c = self._theme.c
        self._console.print(f"[{c('tool_ok')}]  ↓  files presented:[/{c('tool_ok')}]")
        for p in media:
            self._console.print(f"     {p}", markup=False, style="bright_black")
```

No fake event class needed. The `media` field is just part of the existing
`tool_result` event dict.

---

### 8. `bridges/matrix/__main__.py` (follow-up)

Same pattern as Discord: check `event.media` on `AgentToolResult`, upload each
file via Matrix media upload API after the text reply. Lower priority.

---

## Files Changed Summary

| File | Change |
|------|--------|
| `contracts.py` | Add `media` field to `ToolResult` and `AgentToolResult`; add `MESSAGE_TOOL_PREFIX` constant |
| `agent.py` | Detect `MESSAGE_TOOL_PREFIX` in `_execute_tool`; propagate `media` to `AgentToolResult` |
| `modules/message/__init__.py` | New file |
| `modules/message/__main__.py` | New file — registers always-on `message()` tool |
| `gateway/__main__.py` | Include `media` in existing `tool_result` SSE event serialisation |
| `bridges/discord/__main__.py` | Read `event.media` from `AgentToolResult`; upload files in `_ReplyAccumulator.wait_and_send` |
| `bridges/cli/__main__.py` | Print `media` paths from `tool_result` SSE event |

---

## Order of Implementation

1. `contracts.py` — all other files depend on it
2. `modules/message/` — self-contained, testable in isolation
3. `agent.py` — wire sentinel detection and `media` propagation
4. `gateway/__main__.py` — one-liner addition to `_event_to_dict`
5. `bridges/cli/__main__.py` — print paths from existing SSE handler
6. `bridges/discord/__main__.py` — file upload in accumulator

---

## Comparison with Original Plan

| | Original PLAN | This Plan |
|---|---|---|
| New event types | `AgentFileAttachment` | None (Option A) |
| New sentinel strings | `PRESENT_FILES:` | `MESSAGE_TOOL:` |
| Sentinel carries | path list only | content + path list |
| New fields on `ToolResult` | `presented_paths` | `media` |
| New fields on `AgentToolResult` | none (separate event) | `media` |
| New modules | `modules/present/` | `modules/message/` |
| New SSE event type | `files_presented` | None |
| Bridge changes | All 3 + gateway, new code paths | All 3 + gateway, **existing code paths extended** |
| Agent detection logic | New branch + `run()` yield | New branch only |
| Files changed | 7 | 7 (same count, less indirection) |

The key difference: the original plan routes files through a new first-class
event that every layer must learn about. This plan keeps files attached to the
message that triggered them — the same object that bridges already know how to
deliver.
