"""
contracts.py — Pure data contracts. No logic, no I/O, no imports outside stdlib.
Every other layer imports from here. Never the reverse.

Phase 2 tree refactor
---------------------
SessionKey and ChatType are removed. Lanes are now keyed by node_id (str).
InboundMessage.tail_node_id is required (promoted from optional).
_AgentEventBase carries tail_node_id (str) instead of session_key.
Platform is kept — bridges still have platform identity for event dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union
import uuid


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

class Platform(str, Enum):
    CLI     = "cli"
    DISCORD = "discord"
    MATRIX  = "matrix"
    CRON    = "cron"   # internal platform for scheduled cron jobs
    API     = "api"    # HTTP/SSE API bridge


class ContentType(str, Enum):
    TEXT             = "text"
    MIXED            = "mixed"            # text + attachments
    ATTACHMENT_ONLY  = "attachment_only"  # attachments with no text


def content_type_for(text: str, has_attachments: bool) -> "ContentType":
    """Derive the correct ContentType from message text and attachment presence."""
    if has_attachments and text:
        return ContentType.MIXED
    if has_attachments:
        return ContentType.ATTACHMENT_ONLY
    return ContentType.TEXT


# ---------------------------------------------------------------------------
# Group policy
# ---------------------------------------------------------------------------

class ActivationMode(str, Enum):
    MENTION = "mention"   # respond only when @mentioned or prefix used (default)
    PREFIX  = "prefix"    # respond only when command prefix is present
    ALWAYS  = "always"    # respond to every message in the group


@dataclass(frozen=True)
class GroupPolicy:
    """
    Per-group activation and buffering policy.

    Bridges attach this to InboundMessage for group sessions.
    GroupLane in router.py enforces it — bridges pass raw message text.

    activation:       MENTION | PREFIX | ALWAYS
    trigger_prefix:   command prefix string (e.g. "!")
    bot_mxid:         full @user:server MXID (Matrix) or empty string
    bot_localpart:    @localpart derived from bot_mxid, or empty string
    buffer_timeout_s: seconds to wait before flushing buffered non-trigger
                      messages without a trigger arriving. 0 = disabled.
    """
    activation:       ActivationMode = ActivationMode.MENTION
    trigger_prefix:   str            = "!"
    bot_mxid:         str            = ""
    bot_localpart:    str            = ""
    buffer_timeout_s: float          = 0.0


class AttachmentKind(str, Enum):
    IMAGE    = "image"     # image/* — inline as image_url block (vision models)
    TEXT     = "text"      # text/*, .md, .py, .json etc. — read + inline as text
    DOCUMENT = "document"  # .pdf — Anthropic document block or text-extracted
    BINARY   = "binary"    # everything else — reference only, saved to uploads/


# ---------------------------------------------------------------------------
# User identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UserIdentity:
    """
    Who sent the message.
    user_id is the stable per-platform identifier.
    Bridges are responsible for supplying a consistent user_id.
    """
    platform: Platform
    user_id:  str
    username: str       # Human-readable display name


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Attachment:
    """
    A file attached to an inbound message.
    Bridges populate this; attachments.py decides how to deliver it to the LLM.

    filename  — original filename (used for extension sniffing and uploads/ path)
    data      — raw bytes
    mime_type — MIME type as reported by the bridge (e.g. 'image/png', 'application/pdf')
    kind      — classified by attachments.py after construction
    """
    filename:  str
    data:      bytes
    mime_type: str
    kind:      AttachmentKind = AttachmentKind.BINARY


# ---------------------------------------------------------------------------
# Inbound message envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InboundMessage:
    """
    Canonical message produced by bridges.

    tail_node_id  — the cursor node_id for this conversation branch.
                    The router opens or reuses the Lane keyed by this id.
    author        — who sent the message (platform + user_id + display name)
    group_policy  — present for group/channel messages; None for DMs
    """
    tail_node_id: str
    author:       UserIdentity
    content_type: ContentType
    text:         str
    message_id:   str
    timestamp:    float
    reply_to_id:  str | None    = None
    attachments:  tuple["Attachment", ...] = field(default_factory=tuple)
    trace_id:     str           = field(default_factory=lambda: str(uuid.uuid4()))
    group_policy: "GroupPolicy | None" = None  # set by bridge for group messages


# ---------------------------------------------------------------------------
# Agent event stream
#
# AgentLoop.run() yields a stream of AgentEvent objects. The router dispatches
# each event to the correct bridge via per-cursor or per-platform handlers.
# Bridges receive the full event stream and decide what to render.
#
# All events share:
#   tail_node_id         — cursor node_id that identifies the lane
#   trace_id             — ties all events for one user message together
#   reply_to_message_id  — the inbound message_id that triggered this turn
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class _AgentEventBase:
    tail_node_id:        str   # current cursor — advances as new DB nodes are written
    lane_node_id:        str | None = None   # original lane key — stable for the lifetime of the lane
    trace_id:            str
    reply_to_message_id: str

    def __post_init__(self) -> None:
        # Back-compat for tests and older helper code that still constructs
        # events with only tail_node_id.
        if self.lane_node_id is None:
            object.__setattr__(self, "lane_node_id", self.tail_node_id)


@dataclass(frozen=True)
class AgentThinkingChunk(_AgentEventBase):
    """One reasoning/thinking token (reasoning_content field). Never stored in context."""
    text: str


@dataclass(frozen=True)
class AgentTextChunk(_AgentEventBase):
    """One streaming text token. is_partial is always True."""
    text: str


@dataclass(frozen=True)
class AgentTextFinal(_AgentEventBase):
    """
    Final (non-streaming) text, or the closing sentinel after a stream.
    text may be empty when it closes a streamed sequence.
    """
    text: str


@dataclass(frozen=True)
class AgentToolCall(_AgentEventBase):
    """A tool call dispatched by the agent during a tool-use cycle."""
    call_id:   str
    tool_name: str
    args:      dict[str, Any]


@dataclass(frozen=True)
class AgentToolResult(_AgentEventBase):
    """The result of a tool call."""
    call_id:   str
    tool_name: str
    output:    str
    is_error:  bool = False


@dataclass(frozen=True)
class AgentError(_AgentEventBase):
    """LLM error or tool-cycle-limit reached."""
    message: str


# Union type used in type hints throughout the codebase.
AgentEvent = Union[AgentThinkingChunk, AgentTextChunk, AgentTextFinal, AgentToolCall, AgentToolResult, AgentError]


# ---------------------------------------------------------------------------
# Sentinel values
# ---------------------------------------------------------------------------

# Returned by the filesystem view() tool when an image file is read.
# Format: IMAGE_BLOCK_PREFIX + "<mime>;<base64data>"
# agent._execute_tool detects this and builds a vision content block.
IMAGE_BLOCK_PREFIX = "IMAGE_BLOCK:"


# ---------------------------------------------------------------------------
# Tool call / result envelopes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    call_id:   str
    tool_name: str
    args:      dict[str, Any]

    @staticmethod
    def make(tool_name: str, args: dict[str, Any]) -> ToolCall:
        return ToolCall(call_id=str(uuid.uuid4()), tool_name=tool_name, args=args)


@dataclass(frozen=True)
class ToolResult:
    call_id:   str
    tool_name: str
    output:    str
    is_error:  bool = False
    is_image:  bool = False  # True when image_mime + image_b64 are populated
    image_mime: str | None = None  # e.g. "image/jpeg"
    image_b64:  str | None = None  # raw base64, no data URI prefix
