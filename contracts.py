"""
contracts.py — Pure data contracts. No logic, no I/O, no imports outside stdlib.
Every other layer imports from here. Never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import uuid


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

class Platform(str, Enum):
    CLI     = "cli"
    DISCORD = "discord"
    MATRIX  = "matrix"


class ContentType(str, Enum):
    TEXT = "text"


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
# Inbound message envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InboundMessage:
    """
    Canonical message produced by bridges. Nothing platform-specific leaks here.
    """
    session_key:  SessionKey
    author:       UserIdentity
    content_type: ContentType
    text:         str
    message_id:   str
    timestamp:    float
    reply_to_id:  str | None = None
    trace_id:     str = field(default_factory=lambda: str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Outbound reply envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OutboundReply:
    """
    Produced by AgentLoop. The gateway routes on session_key to the
    correct bridge. Bridges never see each other's replies.
    """
    session_key:         SessionKey
    text:                str
    reply_to_message_id: str
    trace_id:            str
    is_partial:          bool = False


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