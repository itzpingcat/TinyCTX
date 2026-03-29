"""
contracts.py — Pure data contracts. No logic, no I/O, no imports outside stdlib.
Every other layer imports from here. Never the reverse.
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


class ChatType(str, Enum):
    DM      = "dm"       # 1-on-1 — session shared across platforms
    GROUP   = "group"    # group/public chat — session is platform-specific


# ---------------------------------------------------------------------------
# Session identity
#
# DM sessions:    SessionKey(chat_type=DM,    conversation_id=<owner_user_id>)
#   platform is None — DMs are platform-agnostic by design.
#   The same human on Discord and Matrix shares one session.
#
# Group sessions: SessionKey(chat_type=GROUP, conversation_id=<channel/room_id>,
#                            platform=<platform>)
#   Group chats are platform-specific — a Discord server and a Matrix room
#   are different contexts.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionKey:
    """Immutable key that uniquely identifies one conversation lane."""
    chat_type:       ChatType
    conversation_id: str        # DM: owner user_id  |  Group: channel/room id
    platform:        Platform | None = None  # None for DMs, required for groups

    def __post_init__(self) -> None:
        if self.chat_type == ChatType.GROUP and self.platform is None:
            raise ValueError("GROUP sessions must specify a platform.")

    def __str__(self) -> str:
        if self.chat_type == ChatType.DM:
            return f"dm:{self.conversation_id}"
        return f"group:{self.platform.value}:{self.conversation_id}"

    @staticmethod
    def dm(owner_user_id: str) -> SessionKey:
        """1-on-1 session key — platform agnostic."""
        return SessionKey(chat_type=ChatType.DM, conversation_id=owner_user_id)

    @staticmethod
    def group(platform: Platform, channel_id: str) -> SessionKey:
        """Group/public session key — platform specific."""
        return SessionKey(chat_type=ChatType.GROUP, conversation_id=channel_id, platform=platform)


@dataclass(frozen=True)
class UserIdentity:
    """
    Who sent the message.
    For DM session routing, user_id is the stable cross-platform owner id.
    Bridges are responsible for mapping their platform user id to this.
    """
    platform: Platform
    user_id:  str       # Stable id used as DM session key
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
    Canonical message produced by bridges. Nothing platform-specific leaks here.

    For group sessions, bridges should set group_policy so the router's
    GroupLane can enforce trigger detection, stripping, and buffering.
    Bridges pass raw (unstripped) text; GroupLane handles everything.

    tail_node_id: tree-refactor field. When set, the router routes this
    message to the lane keyed by this node_id rather than session_key.
    Bridges set this once the tree refactor is complete (Phase 2). Until
    then, session_key is the primary routing key.
    """
    session_key:   SessionKey
    author:        UserIdentity
    content_type:  ContentType
    text:          str
    message_id:    str
    timestamp:     float
    reply_to_id:   str | None    = None
    attachments:   tuple["Attachment", ...] = field(default_factory=tuple)
    trace_id:      str           = field(default_factory=lambda: str(uuid.uuid4()))
    group_policy:  "GroupPolicy | None" = None  # set by bridge for group messages
    tail_node_id:  str | None    = None          # tree refactor: cursor node_id


# ---------------------------------------------------------------------------
# Agent event stream
#
# AgentLoop.run() yields a stream of AgentEvent objects. The gateway routes
# each event to the correct bridge via per-session or per-platform handlers.
# Bridges receive the full event stream and decide what to render.
#
# All events share four common fields:
#   session_key          — which lane produced the event
#   trace_id             — ties all events for one user message together
#   reply_to_message_id  — the inbound message_id that triggered this turn
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class _AgentEventBase:
    session_key:         SessionKey
    trace_id:            str
    reply_to_message_id: str
    tail_node_id:        str | None = None  # tree refactor: tail node after this turn


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