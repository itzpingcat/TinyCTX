"""
contracts.py — Pure data contracts. No logic, no I/O, no imports outside stdlib.
Every other layer imports from here. Never the reverse.

Phase 2 tree refactor
---------------------
SessionKey and ChatType are removed. Lanes are now keyed by node_id (str).
InboundMessage.tail_node_id is required (promoted from optional).
_AgentEventBase carries tail_node_id (str) instead of session_key.
Platform is kept — bridges still have platform identity for event dispatch.

Phase 1 runtime refactor
------------------------
GroupPolicy and ActivationMode removed — trigger detection is bridge-local.
InboundMessage gains trigger: bool = True.
_AgentEventBase loses lane_node_id — tail_node_id is the only cursor.
Platform gains SYSTEM for internal/module-generated messages.
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
    CLI      = "cli"
    DISCORD  = "discord"
    MATRIX   = "matrix"
    TELEGRAM = "telegram"
    CRON     = "cron"   # internal platform for scheduled cron jobs
    API      = "api"    # HTTP/SSE API bridge
    SYSTEM   = "system" # internal platform for module/runtime-generated messages


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



class AttachmentKind(str, Enum):
    IMAGE    = "image"     # image/* — inline as image_url block (vision models)
    TEXT     = "text"      # text/*, .md, .py, .json etc. — read + inline as text
    DOCUMENT = "document"  # .pdf — Anthropic document block or text-extracted
    BINARY   = "binary"    # everything else — reference only, saved to uploads/


# ---------------------------------------------------------------------------
# User identity
# ---------------------------------------------------------------------------

# NOTE: UserIdentity is deprecated. Use TinyCTX.users.User instead.
# Kept here temporarily so existing imports don't break during transition.
@dataclass(frozen=True)
class UserIdentity:
    """
    Deprecated. Bridges now receive a User from UserStore.resolve_user().
    TODO: remove once all bridges are migrated.
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
# Session environment — describes the environment a message arrived in.
# Carried by InboundMessage; snapshotted into state_delta by runtime.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionEnvironment:
    platform:     Platform
    agent_name:   str | None = None
    server_name:  str | None = None
    channel_name: str | None = None


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
    env           — session environment (platform, agent name, server, channel)
    group_policy  — present for group/channel messages; None for DMs
    """
    tail_node_id: str
    author:       Any             # TinyCTX.users.User; typed as Any to avoid circular import
    env:          SessionEnvironment
    content_type: ContentType
    text:         str
    message_id:   str
    timestamp:    float
    trigger:      bool          = True
    reply_to_id:  str | None    = None
    reply_to_author: str | None = None
    attachments:  tuple["Attachment", ...] = field(default_factory=tuple)
    trace_id:     str           = field(default_factory=lambda: str(uuid.uuid4()))


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
    trace_id:            str
    reply_to_message_id: str


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
    suppressed is True when the agent replied with the NO_REPLY sentinel —
    bridges should discard any buffered/streamed text and send nothing.
    """
    text: str
    suppressed: bool = False


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


@dataclass(frozen=True)
class AgentOutboundFiles(_AgentEventBase):
    """
    Emitted by the present() tool when the agent wants to deliver files to
    the user. Appended to agent.outbound_events and yielded immediately after
    the AgentToolResult for that tool call, flowing through the normal event
    stream like any other event. Bridges send each path as a file attachment.
    """
    paths: tuple[str, ...]


# Union type used in type hints throughout the codebase.
AgentEvent = Union[
    AgentThinkingChunk, AgentTextChunk, AgentTextFinal,
    AgentToolCall, AgentToolResult, AgentError, AgentOutboundFiles,
]


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


# ---------------------------------------------------------------------------
# Bridge launch sentinels
# ---------------------------------------------------------------------------

# Bridges that set MANUAL_LAUNCH = True at module level are skipped by
# main.py's auto-start loop. They must be launched explicitly (e.g. via
# `tinyctx launch cli`).
MANUAL_LAUNCH_ATTR = "MANUAL_LAUNCH"
