from __future__ import annotations
import json
import uuid
import importlib
import pkgutil
import sys
from pathlib import Path
from collections import defaultdict


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

USER = "user"
ASSISTANT = "assistant"
TOOL = "tool"
SYSTEM = "system"

FROM_BEGINNING = "from_beginning"
FROM_END = "from_end"

# Hook stages, in order of execution during assemble()
HOOK_PRE_ASSEMBLE   = "pre_assemble"    # fn(ctx) -> None
HOOK_FILTER_TURN    = "filter_turn"     # fn(turn, age, ctx) -> bool  — False drops the turn
HOOK_TRANSFORM_TURN = "transform_turn"  # fn(turn, age, ctx) -> turn | None
HOOK_POST_ASSEMBLE  = "post_assemble"   # fn(messages, ctx) -> messages | None


# ---------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------

class Context:
    """
    Minimal context compiler. Core does almost nothing on its own.

    All optimization — dedup, trimming, truncation, summarization, RAG,
    knowledge graph, etc. — is implemented as modules that hook into
    the assembly pipeline.

    Hook stages (in order):
      pre_assemble    — called once before assembly begins; modules
                        can mutate ctx.dialogue or prepare cached state
      filter_turn     — called per turn; return False to drop it entirely
      transform_turn  — called per turn after filtering; return a new turn
                        dict to replace it, or None to leave it unchanged
      post_assemble   — called once on the final message list; return a
                        new list to replace it, or None to leave unchanged

    Prompt providers:
      modules register fn(ctx) -> str | None via register_prompt().
      Called fresh every assemble(). Returning None skips injection.
      Extension owns whether/what; core owns where.
    """

    def __init__(self, config: dict | None = None, modules_path: str | None = None):
        cfg = config or {}
        self.cfg = {
            "token_budget":        cfg.get("token_budget", 16000),
            # per-extension config overrides: {"ext_name": {key: val}}
            "modules":          cfg.get("modules", {}),
            # names of modules to skip entirely
            "disabled_modules": cfg.get("disabled_modules", []),
        }

        self.dialogue: list[dict] = []

        # pid -> slot metadata (position, depth, priority, role)
        self.prompt_slots: dict[str, dict] = {}
        # pid -> callable(ctx) -> str | None
        self.prompt_providers: dict[str, callable] = {}

        # stage -> [(priority, insertion_order, fn), ...]
        self._hooks: dict[str, list] = defaultdict(list)
        self._hook_counter = 0  # stable tiebreak for equal priorities

        self.state = {"tokens_used": 0}
        self._modules: dict[str, object] = {}

        if modules_path:
            self._load_modules(modules_path)

    # -----------------------------------------------------------------
    # Hook registration
    # -----------------------------------------------------------------

    def register_hook(self, stage: str, fn, *, priority: int = 0):
        """
        Register fn at a pipeline stage.

        Lower priority = runs first within a stage.
        Valid stages: pre_assemble, filter_turn, transform_turn, post_assemble.
        """
        self._hook_counter += 1
        self._hooks[stage].append((priority, self._hook_counter, fn))
        self._hooks[stage].sort(key=lambda x: (x[0], x[1]))

    def unregister_hook(self, stage: str, fn):
        self._hooks[stage] = [
            entry for entry in self._hooks[stage] if entry[2] is not fn
        ]

    # -----------------------------------------------------------------
    # Prompt provider registration
    # -----------------------------------------------------------------

    def register_prompt(
        self,
        pid: str,
        provider_fn,
        *,
        role=SYSTEM,
        position=FROM_BEGINNING,
        depth=0,
        priority=0,
    ):
        """
        Register a prompt provider.

        provider_fn(ctx) -> str | None  — called every assemble().
        Returning None means "don't inject this turn."
        Extension owns whether/what; core owns where.
        """
        self.prompt_slots[pid] = {
            "role":     role,
            "position": position,
            "depth":    depth,
            "priority": priority,
        }
        self.prompt_providers[pid] = provider_fn

    def unregister_prompt(self, pid: str):
        self.prompt_slots.pop(pid, None)
        self.prompt_providers.pop(pid, None)

    # -----------------------------------------------------------------
    # Dialogue
    # -----------------------------------------------------------------

    def add(self, role, content="", *, tool_calls=None, tool_call_id=None):
        turn = {
            "id":           str(uuid.uuid4()),
            "role":         role,
            "content":      content or "",
            "tool_calls":   tool_calls or [],
            "tool_call_id": tool_call_id,
            "index":        len(self.dialogue),
        }
        self.dialogue.append(turn)
        return turn

    def user(self, content):
        return self.add(USER, content)

    def assistant(self, content="", tool_calls=None):
        return self.add(ASSISTANT, content, tool_calls=tool_calls)

    def tool(self, call_id, content):
        return self.add(TOOL, content, tool_call_id=call_id)

    # -----------------------------------------------------------------
    # Assembly
    # -----------------------------------------------------------------

    def assemble(self) -> list[dict]:
        n = len(self.dialogue)

        # ---- pre_assemble ----
        for _, _, fn in self._hooks[HOOK_PRE_ASSEMBLE]:
            fn(self)

        # ---- resolve prompt providers ----
        resolved: dict[str, str] = {}
        for pid, fn in self.prompt_providers.items():
            try:
                content = fn(self)
            except Exception as e:
                print(f"[context] prompt provider '{pid}' raised: {e}")
                content = None
            if content is not None:
                resolved[pid] = content

        # ---- partition prompts ----
        system_parts: list[tuple] = []  # (priority, role, content)
        positional:   list[tuple] = []  # (insert_before_index, priority, role, content)

        for pid, content in resolved.items():
            slot = self.prompt_slots[pid]
            if slot["position"] == FROM_BEGINNING and slot["depth"] == 0:
                system_parts.append((slot["priority"], slot["role"], content))
            else:
                idx = (
                    max(0, slot["depth"] - 1)
                    if slot["position"] == FROM_BEGINNING
                    else max(0, n - slot["depth"])
                )
                positional.append((idx, slot["priority"], slot["role"], content))

        system_parts.sort(key=lambda x: x[0])
        positional.sort(key=lambda x: (x[0], x[1]))

        # ---- build message list ----
        messages: list[dict] = []

        # system block (depth=0, FROM_BEGINNING)
        if system_parts:
            sys_lines = [c for _, r, c in system_parts if r == SYSTEM]
            if sys_lines:
                messages.append({"role": SYSTEM, "content": "\n\n".join(sys_lines)})
            for _, r, c in system_parts:
                if r != SYSTEM:
                    messages.append({"role": r, "content": c})

        pos_i = 0

        for turn in self.dialogue:
            age = n - 1 - turn["index"]

            # flush positional prompts due at this index
            while pos_i < len(positional) and positional[pos_i][0] == turn["index"]:
                _, _, role, content = positional[pos_i]
                messages.append({"role": role, "content": content})
                pos_i += 1

            # ---- filter_turn ----
            dropped = False
            for _, _, fn in self._hooks[HOOK_FILTER_TURN]:
                if fn(turn, age, self) is False:
                    dropped = True
                    break
            if dropped:
                continue

            # ---- transform_turn ----
            for _, _, fn in self._hooks[HOOK_TRANSFORM_TURN]:
                result = fn(turn, age, self)
                if result is not None:
                    turn = result

            messages.append(self._render_turn(turn))

        # trailing positional prompts
        while pos_i < len(positional):
            _, _, role, content = positional[pos_i]
            messages.append({"role": role, "content": content})
            pos_i += 1

        # ---- post_assemble ----
        for _, _, fn in self._hooks[HOOK_POST_ASSEMBLE]:
            result = fn(messages, self)
            if result is not None:
                messages = result

        # ---- merge adjacent same-role messages ----
        merged: list[dict] = []
        for m in messages:
            if (
                merged
                and m["role"] == merged[-1]["role"]
                and m["role"] in (USER, ASSISTANT)
                and "tool_calls" not in m
                and "tool_calls" not in merged[-1]
            ):
                merged[-1]["content"] = (
                    merged[-1]["content"] + "\n\n" + m["content"]
                ).strip()
            else:
                merged.append(dict(m))

        total_chars = sum(len(str(m.get("content", ""))) for m in merged)
        self.state["tokens_used"] = total_chars // 4

        return merged

    def _render_turn(self, turn: dict) -> dict:
        """Convert an internal turn dict to an API-ready message dict."""
        role = turn["role"]

        if role == TOOL:
            return {
                "role":         TOOL,
                "content":      turn["content"],
                "tool_call_id": turn["tool_call_id"],
            }

        if role == ASSISTANT:
            msg: dict = {"role": ASSISTANT, "content": turn["content"]}
            if turn.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id":   tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"]),  # ← FIX
                        },
                    }
                    for tc in turn["tool_calls"]
                ]
            return msg

        return {"role": role, "content": turn["content"]}

    # -----------------------------------------------------------------
    # Extension loading
    # -----------------------------------------------------------------

    def _load_modules(self, modules_path: str):
        path = Path(modules_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"modules path not found: {path}")

        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))

        pkg_name = path.name
        disabled = set(self.cfg["disabled_modules"])

        for _, name, is_pkg in pkgutil.iter_modules([str(path)]):
            if not is_pkg:
                continue  # modules must be packages, not bare .py files

            if name in disabled:
                print(f"[context] '{name}' disabled, skipping")
                continue

            # __init__ — metadata + default config
            try:
                init_mod = importlib.import_module(f"{pkg_name}.{name}")
            except ImportError as e:
                print(f"[context] failed to import {pkg_name}.{name}: {e}")
                continue

            meta        = getattr(init_mod, "EXTENSION_META", {})
            default_cfg = meta.get("default_config", {})
            user_cfg    = self.cfg["modules"].get(name, {})
            ext_config  = {**default_cfg, **user_cfg}

            # __main__ — logic + register()
            try:
                main_mod = importlib.import_module(f"{pkg_name}.{name}.__main__")
            except ImportError as e:
                print(f"[context] failed to import {pkg_name}.{name}.__main__: {e}")
                continue

            if not hasattr(main_mod, "register"):
                print(f"[context] '{name}' missing register() in __main__, skipping")
                continue

            try:
                main_mod.register(self, ext_config)
                self._modules[name] = main_mod
                print(f"[context] loaded '{name}' v{meta.get('version', '?')}")
            except Exception as e:
                print(f"[context] '{name}' register() failed: {e}")