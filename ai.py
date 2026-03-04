"""
ai.py — Async OpenAI-compatible LLM client.
Streams SSE, assembles tool call deltas, yields typed events.
Imports only aiohttp and stdlib. No internal project imports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Any
import aiohttp


# ---------------------------------------------------------------------------
# Yield types
# ---------------------------------------------------------------------------

@dataclass
class TextDelta:
    text: str

@dataclass
class ToolCallAssembled:
    """Emitted once per tool call, after all argument chunks are assembled."""
    call_id:   str
    tool_name: str
    args:      dict[str, Any]

@dataclass
class LLMError:
    message: str


LLMEvent = TextDelta | ToolCallAssembled | LLMError


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LLM:
    """
    Async OpenAI-compatible streaming client.
    Works with Anthropic (via OpenAI-compat endpoint), OpenAI, OpenRouter,
    LM Studio, Ollama, or any base_url that speaks /v1/chat/completions.
    """

    def __init__(
        self,
        base_url:    str,
        api_key:     str,
        model:       str,
        max_tokens:  int   = 2048,
        temperature: float = 0.7,
        timeout:     int   = 60,
    ) -> None:
        self.model       = model
        self.endpoint    = f"{base_url.rstrip('/')}/chat/completions"
        self.api_key     = api_key
        self.max_tokens  = max_tokens
        self.temperature = temperature
        self.timeout     = aiohttp.ClientTimeout(total=timeout)

    async def stream(
        self,
        messages: list[dict],
        tools:    list[dict] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """
        Stream a completion. Yields TextDelta, ToolCallAssembled, or LLMError.
        Tool call argument chunks are assembled before yielding — callers
        always receive complete, parseable args dicts.
        """
        payload: dict[str, Any] = {
            "model":       self.model,
            "messages":    messages,
            "temperature": self.temperature,
            "max_tokens":  self.max_tokens,
            "stream":      True,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # Accumulate tool call fragments keyed by index
        # { index: {"id": str, "name": str, "args_buf": str} }
        tool_buf: dict[int, dict] = {}
        # in LLM.stream(), right before the aiohttp call
        # print(f"[debug] hitting: {self.endpoint}")
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    self.endpoint, headers=headers, json=payload
                ) as resp:
                    # print(f"[debug] status: {resp.status}")
                    if resp.status != 200:
                        body = await resp.text()
                        yield LLMError(f"HTTP {resp.status}: {body}")
                        return

                    async for raw in resp.content:
                        # print(f"[debug] raw: {raw}")
                        line = raw.decode().strip()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        choices = data.get("choices")
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})

                        # Text content
                        if text := delta.get("content"):
                            yield TextDelta(text=text)

                        # Tool call fragments — assemble before yielding
                        for tc in delta.get("tool_calls", []):
                            idx = tc.get("index", 0)
                            if idx not in tool_buf:
                                tool_buf[idx] = {"id": "", "name": "", "args_buf": ""}
                            buf = tool_buf[idx]
                            if tc.get("id"):
                                buf["id"] = tc["id"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                buf["name"] = fn["name"]
                            buf["args_buf"] += fn.get("arguments", "")

                    # Stream done — emit assembled tool calls
                    for buf in tool_buf.values():
                        try:
                            args = json.loads(buf["args_buf"] or "{}")
                        except json.JSONDecodeError:
                            args = {"_raw": buf["args_buf"]}
                        yield ToolCallAssembled(
                            call_id=buf["id"],
                            tool_name=buf["name"],
                            args=args,
                        )

        except aiohttp.ClientConnectionError as e:
            yield LLMError(f"Connection failed: {e}")
        except Exception as e:
            yield LLMError(str(e))