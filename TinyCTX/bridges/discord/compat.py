"""
bridges/discord/compat.py — Proxy-bot compatibility rules.

Loads bridges/discord/compat.json and matches incoming Discord messages against
a list of per-pattern delay rules. Each rule specifies match conditions and a
delay_s. When a non-webhook message matches, it is held for delay_s then verified
via fetch_message — if deleted (proxied), it is dropped and the webhook repost
is handled instead.

Match fields (all optional, ANDed together):
  content_regex   — regex tested against message content
  author_id       — exact Discord user ID (integer) of the sender
  has_webhook_id  — if true, only match messages that ARE webhooks

Example compat.json:
  [
    {
      "description": "Tupperbot proxy",
      "match": { "content_regex": "^\\w+:.+" },
      "delay_s": 0.8
    }
  ]
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import discord

logger = logging.getLogger(__name__)


class CompatRules:
    """
    Loads bridges/discord/compat.json and matches incoming messages against
    a list of rules. Each rule specifies match conditions and a delay_s.
    All match fields within a rule are ANDed. First matching rule wins.
    File is hot-reloaded whenever its mtime changes.
    """

    def __init__(self, path: Path) -> None:
        self._path  = path
        self._rules: list[dict] = []
        self._mtime: float      = 0.0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._rules = []
            return
        try:
            mtime = self._path.stat().st_mtime
            if mtime == self._mtime:
                return
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            # Pre-compile regexes.
            compiled = []
            for rule in raw:
                entry = dict(rule)
                m = entry.get("match", {})
                if "content_regex" in m:
                    entry["_content_re"] = re.compile(m["content_regex"])
                compiled.append(entry)
            self._rules = compiled
            self._mtime = mtime
            logger.info(
                "Discord compat: loaded %d rule(s) from %s",
                len(self._rules), self._path,
            )
        except Exception:
            logger.exception("Discord compat: failed to load %s", self._path)

    def match(self, message: discord.Message) -> float:
        """
        Return the delay_s for the first matching rule, or 0.0 if no rule matches.
        Reloads the file if it has changed on disk.
        """
        self._load()
        for rule in self._rules:
            m = rule.get("match", {})
            if "content_regex" in m:
                if not rule["_content_re"].search(message.content):
                    continue
            if "author_id" in m:
                if message.author.id != int(m["author_id"]):
                    continue
            if "has_webhook_id" in m:
                want = bool(m["has_webhook_id"])
                if bool(message.webhook_id) != want:
                    continue
            return float(rule.get("delay_s", 0.0))
        return 0.0
