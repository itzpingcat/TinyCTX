"""
bridges/telegram/__main__.py — Telegram bridge (TOOLS_BRIDGES.md).

Transport only: normalizes Telegram messages to InboundMessage and renders
reply text back — no pipeline logic here. Long-polls getUpdates with the raw
Bot API (aiohttp; no extra dependency).

Behavior:
  - Private chats: every text message triggers a turn.
  - Groups: only messages that address the bot trigger — by its @username, its
    display name, or a configured alias ("Eve", "@Eve"), OR by being a direct
    reply to one of the bot's own messages. Name/mention tokens are stripped;
    replies keep their full text. Everything else is ignored (not recorded).
    Bare-name/alias matching needs privacy mode disabled in @BotFather (else the
    Bot API only delivers real @username mentions and replies-to-bot).
  - Edits: edited messages are received too (allowed_updates includes
    edited_message) and re-run through the same gate, prefixed "(edited
    message)" so Eve knows the user revised an earlier turn.
  - Attachments: not yet supported — text in/out only (per TOOLS_BRIDGES.md).

Config (config.yaml):
    bridges:
      telegram:
        enabled: true
        options:
          token_env: TELEGRAM_BOT_TOKEN   # env var holding the bot token
          allowed_users: []               # Telegram user IDs; empty = open
          max_reply_length: 4096          # Telegram hard limit per message
          api_base: https://api.telegram.org   # overridable for tests
          mention_aliases: []             # extra names to answer to in groups;
                                          # the bot's display name is added auto

Cursors persist per chat in workspace/cursors/telegram.json so sessions
survive restarts (same pattern as the Discord bridge).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from TinyCTX.bridges.telegram.api import DEFAULT_API_BASE, TelegramAPI
from TinyCTX.contracts import (
    AgentError, AgentTextChunk, AgentTextFinal, InboundMessage, Platform,
    SessionEnvironment, content_type_for,
)

logger = logging.getLogger(__name__)


class TelegramBridge:
    def __init__(self, runtime, options: dict) -> None:
        self.runtime = runtime
        self.token_env = options.get("token_env", "TELEGRAM_BOT_TOKEN")
        self.allowed_users = {str(u) for u in options.get("allowed_users", [])}
        self.max_reply_length = int(options.get("max_reply_length", 4096))
        self.api_base = options.get("api_base", DEFAULT_API_BASE)
        # Extra names the bot answers to in groups (besides its @username).
        # The bot's display name is added automatically in run(); Telegram bot
        # usernames must end in "bot", so this lets her respond to "Eve"/"@Eve".
        self.mention_aliases = {
            str(a).strip().lstrip("@").lower()
            for a in options.get("mention_aliases", [])
            if str(a).strip()
        }
        self.bot_name = ""   # display name (first_name), set in run()
        self.bot_id = 0      # numeric bot id, set in run() (reply-author match)

        workspace = Path(runtime.config.workspace.path).expanduser().resolve()
        cursors_dir = workspace / "cursors"
        cursors_dir.mkdir(parents=True, exist_ok=True)
        self._cursor_file = cursors_dir / "telegram.json"
        self._cursors: dict[str, str] = self._load_cursors()
        self._chat_locks: dict[str, asyncio.Lock] = {}

        self.api: TelegramAPI | None = None
        self.bot_username = ""

    # -------------------------------------------------------------- cursors

    def _load_cursors(self) -> dict[str, str]:
        try:
            return json.loads(self._cursor_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cursors(self) -> None:
        try:
            self._cursor_file.write_text(json.dumps(self._cursors, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("[telegram] failed to save cursors")

    def _cursor_for(self, chat_key: str) -> str:
        node_id = self._cursors.get(chat_key)
        if node_id and self.runtime.db.get_node(node_id):
            return node_id
        root = self.runtime.db.get_root()
        node = self.runtime.db.add_node(
            parent_id=root.id, role="system", content=f"session:{chat_key}",
        )
        self._cursors[chat_key] = node.id
        self._save_cursors()
        return node.id

    # -------------------------------------------------------------- main loop

    async def run(self) -> None:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            # Park instead of returning: main.py treats any bridge task
            # completing as an app-wide shutdown signal, and a misconfigured
            # bridge must not take down the gateway.
            logger.error(
                "[telegram] no token in $%s — bridge idle (set the env var and restart)",
                self.token_env,
            )
            await asyncio.Event().wait()
            return

        self.api = TelegramAPI(token, api_base=self.api_base)
        me = await self.api.get_me()
        self.bot_username = me.get("username", "")
        self.bot_id = int(me.get("id", 0) or 0)
        self.bot_name = (me.get("first_name") or "").strip()
        if self.bot_name:
            self.mention_aliases.add(self.bot_name.lower())
        logger.info(
            "[telegram] connected as @%s (answers to: %s)",
            self.bot_username,
            ", ".join(sorted({f"@{self.bot_username}", *self.mention_aliases})),
        )

        offset = 0
        try:
            while True:
                try:
                    updates = await self.api.get_updates(offset)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("[telegram] getUpdates failed (%s) — retrying in 5s", exc)
                    await asyncio.sleep(5)
                    continue
                for update in updates:
                    offset = max(offset, int(update.get("update_id", 0)) + 1)
                    message = update.get("message")
                    if message:
                        asyncio.create_task(self._handle_message(message))
                        continue
                    edited = update.get("edited_message")
                    if edited:
                        asyncio.create_task(self._handle_message(edited, is_edit=True))
        finally:
            await self.api.close()

    # -------------------------------------------------------------- inbound

    def _accepts(self, message: dict) -> str | None:
        """Return the effective text if this message should trigger, else None."""
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return None
        text = (message.get("text") or message.get("caption") or "").strip()
        if not text:
            return None
        if self.allowed_users and str(sender.get("id")) not in self.allowed_users:
            return None
        chat = message.get("chat") or {}
        if chat.get("type") != "private" and not self._is_reply_to_bot(message):
            # In a group, a direct reply to one of Eve's messages counts as
            # addressing her (whole text kept); otherwise require a name/mention.
            addressed = self._strip_group_address(text)
            if addressed is None:
                return None
            text = addressed
            if not text:
                return None
        return text

    def _is_reply_to_bot(self, message: dict) -> bool:
        """True when this message is a Telegram reply to one of Eve's messages."""
        author = (message.get("reply_to_message") or {}).get("from") or {}
        if not author.get("is_bot"):
            return False
        if self.bot_id and author.get("id") == self.bot_id:
            return True
        return bool(self.bot_username) and \
            author.get("username", "").lower() == self.bot_username.lower()

    def _strip_group_address(self, text: str) -> str | None:
        """
        In a group, the bot only replies when addressed — by its real
        @username, or by any alias/display name ("Eve", "@Eve"). Returns the
        text with the matched address token(s) removed, or None if the bot was
        not addressed. Bare-name matching requires privacy mode disabled in
        @BotFather so the bot actually receives non-@username group messages.
        """
        names = set(self.mention_aliases)
        if self.bot_username:
            names.add(self.bot_username.lower())
        if not names:
            return None

        matched = False
        out = text
        # Longest first so a full "@username" wins before any short alias.
        for name in sorted(names, key=len, reverse=True):
            pattern = re.compile(rf"@?\b{re.escape(name)}\b", re.IGNORECASE)
            if pattern.search(out):
                matched = True
                out = pattern.sub("", out)
        if not matched:
            return None
        return re.sub(r"\s{2,}", " ", out).strip()

    async def _handle_message(self, message: dict, is_edit: bool = False) -> None:
        text = self._accepts(message)
        if text is None:
            return

        chat = message["chat"]
        sender = message["from"]
        if is_edit:
            # Mark edits so Eve knows the user revised an earlier message rather
            # than sending a fresh one, and it's visible in the logs.
            logger.info("[telegram] edited message from %s in %s",
                        sender.get("id"), chat.get("id"))
            text = f"(edited message) {text}"
        chat_key = f"tg:{chat['id']}"
        lock = self._chat_locks.setdefault(chat_key, asyncio.Lock())

        async with lock:
            user = self.runtime.users.resolve_user(
                platform=Platform.TELEGRAM,
                user_id=str(sender["id"]),
                username=sender.get("username") or f"tg{sender['id']}",
                display_name=" ".join(
                    p for p in (sender.get("first_name"), sender.get("last_name")) if p
                ) or None,
            )
            msg = InboundMessage(
                tail_node_id=self._cursor_for(chat_key),
                author=user,
                env=SessionEnvironment(
                    platform=Platform.TELEGRAM,
                    channel_name=chat.get("title") if chat.get("type") != "private" else None,
                ),
                content_type=content_type_for(text, False),
                text=text,
                message_id=str(message.get("message_id", "")),
                timestamp=float(message.get("date", time.time())),
                trigger=True,
            )

            reply_queue: asyncio.Queue = asyncio.Queue()
            assert self.api is not None
            await self.api.send_chat_action(chat["id"])
            new_tail = await self.runtime.push(msg, reply_queue=reply_queue)

            final_text = ""
            streamed: list[str] = []
            suppressed = False
            while True:
                event = await reply_queue.get()
                if event is None:
                    break
                if isinstance(event, AgentTextChunk):
                    streamed.append(event.text)
                elif isinstance(event, AgentTextFinal):
                    new_tail = event.tail_node_id
                    if event.suppressed:
                        suppressed = True
                    elif event.text:
                        final_text = event.text
                elif isinstance(event, AgentError):
                    logger.error("[telegram] agent error: %s", event.message)
            final_text = "" if suppressed else (final_text or "".join(streamed))

            self._cursors[chat_key] = new_tail
            self._save_cursors()

            for chunk in _chunks(final_text, self.max_reply_length):
                try:
                    await self.api.send_message(chat["id"], chunk)
                except Exception:
                    logger.exception("[telegram] send_message failed")
                    break


def _chunks(text: str, limit: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [text[i:i + limit] for i in range(0, len(text), limit)]


async def run(runtime) -> None:
    """Auto-start entry point (main.py bridge loader)."""
    options = {}
    bridge_cfg = runtime.config.bridges.get("telegram")
    if bridge_cfg is not None:
        options = getattr(bridge_cfg, "options", None) or {}
    bridge = TelegramBridge(runtime, options)
    await bridge.run()
