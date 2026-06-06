from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from TinyCTX.config import Config
from TinyCTX.contracts import InboundMessage
from TinyCTX.users import UserStore
from TinyCTX.utils.attachments import build_content_blocks as _build_content_blocks
from TinyCTX.db import ConversationDB
from TinyCTX.utils.commands import CommandRegistry
from TinyCTX.module_registry import ModuleRegistry

logger = logging.getLogger(__name__)

class Runtime:
    def __init__(self, config: Config) -> None:
        self.config = config
        
        # Shared DB for writing inbound nodes. 
        # AgentCycle will open its own connection for reading/inference.
        workspace = Path(config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self.db = ConversationDB(workspace / "agent.db")

        self.commands = CommandRegistry()
        self.module_registry = ModuleRegistry()
        self.users = UserStore()

        # Concurrency Management
        max_workers = getattr(config, "max_workers", 8)
        self._semaphore = asyncio.Semaphore(max_workers)
        self._active: int = 0
        self._tasks: set[asyncio.Task] = set()
        self._abort_events: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        self._register_user_commands()
        self.module_registry.load_modules(self)
        logger.info("Runtime started")

    def _register_user_commands(self) -> None:
        """
        Register /user modify_permissions, /user info, and /user rename slash commands.

        /user modify_permissions <username> <level>  — set a user's permission_level
        /user info <username>                        — show a user's stored info
        /user rename <username> <new>                — rename a TinyCTX username (requires caller level 100)

        Permission rules match the agent tool:
          - caller can only promote to at most (their level - 1)
          - caller can only modify users whose current level is at most (their level - 1)
        """
        users = self.users

        def _caller_user(context: dict):
            """Return the invoking User, or None if unresolvable."""
            from TinyCTX.contracts import Platform
            interaction = context.get("interaction")
            if interaction is not None:
                return users.resolve_user(
                    platform=Platform.DISCORD,
                    user_id=str(interaction.user.id),
                    username=interaction.user.name,
                    display_name=interaction.user.display_name,
                )
            return None

        from TinyCTX.users import UsernameConflictError

        async def _cmd_modify_permissions(args: list[str], context: dict) -> None:
            send = context["send"]
            if len(args) < 2:
                await send("Usage: /user modify_permissions <username> <level>")
                return
            caller = _caller_user(context)
            if caller is None:
                await send("⛔ Cannot resolve your identity.")
                return
            target_username = args[0]
            try:
                level = int(args[1])
            except ValueError:
                await send(f"Invalid level {args[1]!r} — must be an integer.")
                return
            if not (0 <= level <= 100):
                await send("Level must be between 0 and 100.")
                return
            max_grantable = caller.permission_level - 1
            if level > max_grantable:
                await send(f"⛔ Cannot set level {level} — you may only grant up to {max_grantable} (your level − 1).")
                return
            user = users.get_user(target_username)
            if user is None:
                await send(f"User {target_username!r} not found.")
                return
            if user.permission_level >= caller.permission_level:
                await send(f"⛔ {target_username!r} is at level {user.permission_level} — not below your level ({caller.permission_level}).")
                return
            old_level = user.permission_level
            user.permission_level = level
            users.update_user(user)
            logger.info(
                "[user] %s set level %d on %s (was %d)",
                caller.username, level, target_username, old_level,
            )
            await send(f"✅ {target_username}: {old_level} → {level}")

        async def _cmd_info(args: list[str], context: dict) -> None:
            send = context["send"]
            if not args:
                await send("Usage: /user info <username>")
                return
            user = users.get_user(args[0])
            if user is None:
                await send(f"User {args[0]!r} not found.")
                return
            identities = ", ".join(
                f"{i.platform.value}:{i.user_id} ({i.username})"
                for i in user.identities
            ) or "none"
            await send(
                f"**{user.username}** — level {user.permission_level}\n"
                f"Identities: {identities}\n"
                f"Created: {user.created_at:.0f}"
            )

        async def _cmd_rename(args: list[str], context: dict) -> None:
            send = context["send"]
            if len(args) < 2:
                await send("Usage: /user rename <username> <new_username>")
                return
            caller = _caller_user(context)
            if caller is None or caller.permission_level < 100:
                await send("⛔ Permission denied. Requires level 100.")
                return
            try:
                updated = users.rename_user(args[0], args[1])
                await send(f"✅ Renamed {args[0]!r} → {updated.username!r}")
            except ValueError as e:
                await send(f"Error: {e}")
            except UsernameConflictError:
                await send(f"Username {args[1]!r} is already taken.")

        self.commands.register("user", "modify_permissions", _cmd_modify_permissions,
            help="Set a user's permission level",
            params=[("username", str, "TinyCTX username"), ("level", int, "Permission level (0-100)")])
        self.commands.register("user", "info", _cmd_info,
            help="Show a user's stored identity and level",
            params=[("username", str, "TinyCTX username")])
        self.commands.register("user", "rename", _cmd_rename,
            help="Rename a TinyCTX username (admin only)",
            params=[("username", str, "Current username"), ("new_username", str, "New username")])

    # ------------------------------------------------------------------
    # Entry Point: push()
    # ------------------------------------------------------------------

    async def push(self, msg: InboundMessage, reply_queue: asyncio.Queue | None = None) -> str:
        """
        Accepts InboundMessage, persists to DB, and triggers AgentCycle if needed.
        Always returns the new user node id.
        If reply_queue is provided and msg.trigger is True, events are written into
        it as they arrive. A None sentinel is put when the turn is complete.
        """
        # 1. Build message content — inline attachments or append reference notes.
        workspace = Path(self.config.workspace.path).expanduser().resolve()
        primary_name = self.config.llm.primary
        model_cfg = self.config.models.get(primary_name)
        effective_text = f"[Replying to {msg.reply_to_author}]\n{msg.text}" if msg.reply_to_author else msg.text
        content = _build_content_blocks(
            text=effective_text,
            attachments=msg.attachments,
            model_cfg=model_cfg,
            att_cfg=self.config.attachments,
            workspace=workspace,
        ) if msg.attachments else effective_text

        # Serialise list content to JSON string for DB storage.
        content_str = json.dumps(content, ensure_ascii=False) if isinstance(content, list) else content

        # 2. Write User Node to DB
        state_delta = self._compute_state_delta(msg)
        user_node = self.db.add_node(
            parent_id=msg.tail_node_id,
            role="user",
            content=content_str,
            author_id=msg.author.username,
            state_delta=json.dumps(state_delta) if state_delta else None,
        )
        
        new_tail_id = user_node.id

        # 4. Trigger Cycle if requested
        if not msg.trigger:
            return new_tail_id

        # Capacity Check
        if self._active >= (self._semaphore._value + self._active):
            logger.warning("Capacity reached. Node %s persisted but not triggered.", new_tail_id)
            if reply_queue is not None:
                await reply_queue.put(None)
            return new_tail_id

        # Spawn Task
        abort_ev = self._get_abort_event(new_tail_id)
        task = asyncio.create_task(
            self._process(new_tail_id, msg.author.permission_level, abort_ev, reply_queue),
            name=f"cycle:{new_tail_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return new_tail_id

    # ------------------------------------------------------------------
    # Processing Logic
    # ------------------------------------------------------------------

    async def _process(self, node_id: str, permission_level: int, abort_event: asyncio.Event, reply_queue: asyncio.Queue | None = None) -> None:
        from TinyCTX.agent import AgentCycle
        
        async with self._semaphore:
            self._active += 1
            try:
                agent = AgentCycle(self.config, self.module_registry)
                logger.debug("[runtime] cycle starting for node %s", node_id)
                
                async for event in agent.run(node_id, permission_level, abort_event):
                    if reply_queue is not None:
                        await reply_queue.put(event)
                
                logger.debug("[runtime] cycle complete for node %s", node_id)
            except Exception:
                logger.exception("Cycle failed for node %s", node_id)
            finally:
                self._active -= 1
                self._abort_events.pop(node_id, None)
                if reply_queue is not None:
                    await reply_queue.put(None)  # sentinel: turn complete

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _compute_state_delta(self, msg: InboundMessage) -> dict:
        prior_state, _ = self.db.load_session_state(msg.tail_node_id)
        delta = {}
        mapping = {
            "platform": msg.author.identities[0].platform.value if msg.author.identities else None,
            "author_id": msg.author.username,
            "server_name": msg.server_name,
            "channel_name": msg.channel_name
        }
        for k, v in mapping.items():
            if v is not None and prior_state.get(k) != v:
                delta[k] = v
        return delta

    def _get_abort_event(self, node_id: str) -> asyncio.Event:
        ev = self._abort_events.get(node_id) or asyncio.Event()
        ev.clear()
        self._abort_events[node_id] = ev
        return ev
    
    def abort(self, node_id: str) -> bool:
        if node_id in self._abort_events:
            self._abort_events[node_id].set()
            return True
        return False

    async def shutdown(self) -> None:
        for t in self._tasks: t.cancel()
        if self._tasks: await asyncio.gather(*self._tasks, return_exceptions=True)
        self.db.close()