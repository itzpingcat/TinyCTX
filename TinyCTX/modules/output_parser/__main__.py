"""
modules/output_parser/__main__.py

Recovers from a model emitting tool calls as plain TEXT instead of using its
native tool-calling channel — a common small/local-model failure mode (fenced
```tool_call blocks, <tool_call> tags, bare JSON, or Pythonic call syntax).

Hooks HOOK_POST_COMPLETION (context.py), which fires in AgentCycle.run()
right after a completion is assembled — before the empty-completion check
and before the assistant HistoryEntry is written. See context.py's
HOOK_POST_COMPLETION docstring for the exact contract.

Two responses, depending on what was found (ported from little-coder's
.pi/extensions/output-parser, including its issue #42 reasoning):

  - Genuine slip (fenced/tag/bare-JSON): the model has a native tool-calling
    channel and just formatted this one wrong. Queue a follow-up user turn
    naming the calls it seemed to intend and asking it to re-issue them
    natively. Capped at max_nudges_per_cycle — a model that's structurally
    incapable of native calling under the current server config must not be
    nudged forever.

  - Native-but-unparsed (Liquid/Pythonic, e.g. LFM2): this format IS that
    model's real tool-calling channel; a "use native tool calls" nudge would
    just make it re-emit the same text every turn. Surface a one-time
    diagnostic instead, pointing at the actual fix (serving with a chat
    template that parses these into real tool_calls upstream) — notified via
    PostCompletionAction.notify, not repeated every turn.

We never execute an extracted call ourselves — a text-encoded call may be
incomplete or something the model didn't fully mean, and auto-executing it
would bypass permissions.py's capability check that every real tool call
goes through. Detection and description only; the model re-issues it for
real.
"""
from __future__ import annotations

import logging

from TinyCTX.context import PostCompletionAction
from TinyCTX.modules.output_parser.parser import parse_text_tool_calls

logger = logging.getLogger(__name__)


def register_runtime(runtime) -> None:
    pass  # no singletons — all state is per-AgentCycle, see register_agent


def register_agent(cycle) -> None:
    try:
        from TinyCTX.modules.output_parser import EXTENSION_META
        config = EXTENSION_META.get("default_config", {})
    except ImportError:
        config = {}

    if not config.get("enabled", True):
        return

    _register_output_repair(cycle.context, config)


def _register_output_repair(context, config):
    max_nudges = int(config.get("max_nudges_per_cycle", 2))

    # Per-cycle (= per AgentCycle = per session-turn-sequence) state, exactly
    # like ctx_tools' closures above — NOT module-level, so this never leaks
    # across the unrelated sessions a single TinyCTX process may be juggling
    # concurrently (multiple bridges/users).
    state = {"liquid_notified": False, "nudge_count": 0}

    def post_completion(response_text, tool_calls_list, ctx):
        if tool_calls_list:
            return None  # native calling worked this turn — nothing to rescue

        text = response_text or ""
        calls = parse_text_tool_calls(text)
        if not calls:
            return None

        liquid_calls = [c for c in calls if c.format == "liquid"]
        other_calls  = [c for c in calls if c.format != "liquid"]

        notify_parts: list[str] = []
        followup: str | None = None

        if liquid_calls and not state["liquid_notified"]:
            state["liquid_notified"] = True
            names = ", ".join(c.name for c in liquid_calls)
            notify_parts.append(
                f"model emitted {len(liquid_calls)} Pythonic tool call(s) as text "
                f"[{names}] (LFM2/Liquid format). This is that model's native "
                "channel, not a slip — a nudge won't help. Serve with the "
                "model's matching chat template (--jinja) so calls parse into "
                "native tool_calls upstream."
            )
            logger.info(
                "[output_parser] Liquid/Pythonic call(s) detected as text: %s", names
            )

        if other_calls:
            if state["nudge_count"] < max_nudges:
                state["nudge_count"] += 1
                names = ", ".join(c.name for c in other_calls)
                notify_parts.append(
                    f"model wrote {len(other_calls)} tool call(s) as text [{names}] "
                    f"— nudging back to native tool calls (attempt "
                    f"{state['nudge_count']}/{max_nudges})."
                )
                logger.info(
                    "[output_parser] nudging model back to native tool calls: %s", names
                )
                intended = "; ".join(
                    f"{c.name}({c.input!r})" for c in other_calls
                )
                followup = (
                    "Your previous response embedded tool calls inside text "
                    "(e.g. fenced ```tool_call blocks, <tool_call> tags, or bare "
                    "JSON). Please re-issue them as NATIVE tool calls. If the "
                    f"intended calls were: {intended} — execute them now using "
                    "your tool-call channel, not text."
                )
            else:
                names = ", ".join(c.name for c in other_calls)
                notify_parts.append(
                    f"model wrote {len(other_calls)} tool call(s) as text again "
                    f"[{names}] after {max_nudges} nudge(s) — giving up on "
                    "nudging this cycle; the model may not be reliably reaching "
                    "its native tool-call channel under the current config."
                )
                logger.warning(
                    "[output_parser] nudge budget exhausted (%d/%d) — model still "
                    "emitting text-encoded calls: %s",
                    max_nudges, max_nudges, names,
                )

        if not notify_parts:
            return None

        return PostCompletionAction(
            followup_message=followup,
            notify=" | ".join(notify_parts),
        )

    context.register_hook("post_completion", post_completion, priority=0)
