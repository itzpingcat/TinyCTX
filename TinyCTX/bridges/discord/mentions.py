"""
bridges/discord/mentions.py — Mention humanization helpers.

Converts between Discord <@snowflake> mentions and TinyCTX @username strings.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord

from TinyCTX.contracts import Platform

if TYPE_CHECKING:
    from TinyCTX.runtime import Runtime

# Matches @tinyctx-username in outbound text (e.g. "@alice", "@bob_42").
_TINYCTX_MENTION_RE = re.compile(r'@([a-z0-9][a-z0-9_-]{1,31})')


async def humanize_mentions(text: str, client: discord.Client) -> str:
    """Replace <@id> and <@!id> with @username in inbound Discord text."""
    pattern = re.compile(r"<@!?(\d+)>")

    async def _replace(match: re.Match) -> str:
        try:
            user = await client.fetch_user(int(match.group(1)))
            return f"@{user.name}"
        except Exception:
            return f"@[{match.group(1)}]"

    parts: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        parts.append(text[last : m.start()])
        parts.append(await _replace(m))
        last = m.end()
    parts.append(text[last:])
    return "".join(parts)


def dehumanize_mentions(text: str, runtime: "Runtime") -> str:
    """Replace @tinyctx-username with <@discord_snowflake> in outbound text."""
    def _replace(m: re.Match) -> str:
        username = m.group(1)
        user = runtime.users.get_user(username)
        if user is None:
            return m.group(0)
        for ident in user.identities:
            if ident.platform == Platform.DISCORD:
                return f"<@{ident.user_id}>"
        return m.group(0)

    return _TINYCTX_MENTION_RE.sub(_replace, text)
