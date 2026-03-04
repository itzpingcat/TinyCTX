from __future__ import annotations
import json


def register(context, config):
    _register_dedup(context, config)
    _register_trim(context, config)


def _register_dedup(context, config):
    dedup_after = config.get("same_call_dedup_after", 3)

    suppressed_tool:  set[int] = set()
    suppressed_calls: set[str] = set()

    def pre_assemble(ctx):
        suppressed_tool.clear()
        suppressed_calls.clear()

        dialogue = ctx.dialogue
        n = len(dialogue)

        call_map = {
            tc["id"]: tc
            for turn in dialogue
            for tc in turn.get("tool_calls", [])
        }

        seen: set[str] = set()

        for i in reversed(range(n)):
            turn = dialogue[i]
            if turn["role"] != "tool" or not turn["tool_call_id"]:
                continue
            tc = call_map.get(turn["tool_call_id"])
            if not tc:
                continue
            sig = tc["name"] + "::" + json.dumps(tc["arguments"], sort_keys=True)
            age = n - 1 - i
            if sig in seen and age > dedup_after:
                suppressed_tool.add(i)
                suppressed_calls.add(tc["id"])
            else:
                seen.add(sig)

    def filter_turn(turn, age, ctx):
        if turn["role"] == "tool" and turn["index"] in suppressed_tool:
            return False

    def transform_turn(turn, age, ctx):
        if turn["role"] != "assistant":
            return None
        surviving = [
            tc for tc in turn.get("tool_calls", [])
            if tc["id"] not in suppressed_calls
        ]
        if len(surviving) == len(turn.get("tool_calls", [])):
            return None
        if not surviving and not turn["content"].strip():
            return None
        return {**turn, "tool_calls": surviving}

    context.register_hook("pre_assemble",   pre_assemble,   priority=0)
    context.register_hook("filter_turn",    filter_turn,    priority=0)
    context.register_hook("transform_turn", transform_turn, priority=0)


def _register_trim(context, config):
    trim_after     = config.get("tool_trim_after", 10)
    truncate_after = config.get("tool_output_truncate_after", 2)
    max_chars      = config.get("max_tool_output_chars", 2000)

    def transform_turn(turn, age, ctx):
        if turn["role"] != "tool":
            return None

        content = turn["content"]

        if age > trim_after:
            return {**turn, "content": f"[trimmed — tool output, {age} turns ago]"}

        if age > truncate_after and len(content) > max_chars:
            half    = max_chars // 2
            omitted = len(content) - max_chars
            content = (
                content[:half]
                + f"\n... [{omitted} chars omitted] ...\n"
                + content[-half:]
            )
            return {**turn, "content": content}

        return None

    # priority=10 so dedup runs first
    context.register_hook("transform_turn", transform_turn, priority=10)