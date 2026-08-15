from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from TinyCTX.config import Config
from TinyCTX.contracts import AgentError, InboundMessage, SessionEnvironment
from TinyCTX.users import UserStore
from TinyCTX.utils.attachments import build_content_blocks as _build_content_blocks
from TinyCTX.db import ConversationDB
from TinyCTX.utils.commands import CommandRegistry
from TinyCTX.module_registry import ModuleRegistry

logger = logging.getLogger(__name__)

# Digests (see Runtime._render_digest) are capped so a long fork output
# doesn't blow the context budget of every peer it fans out to.
_DIGEST_MAX_CHARS = 2000


@dataclass
class Exogenous:
    """One typed event carried in a Run's inbox — drained into a node by
    AgentCycle at the two R3 drain points. See docs/PLAN.md §3.3."""
    kind:    str    # "fork_finished" | "nudge"
    role:    str    # role to write the node as
    content: str    # pre-rendered


@dataclass
class Run:
    """In-memory handle for one live AgentCycle. Not persisted — see
    docs/PLAN.md §3.1. run.id is independent of any DB node id, which is
    what makes two concurrent runs on one conversation representable."""
    id:            str
    session_key:   str
    intent:        str
    root_node_id:  str
    status:        str = "running"          # running | done | failed | aborted
    started_at:    float = field(default_factory=time.time)
    inbox:         asyncio.Queue = field(default_factory=asyncio.Queue)
    cycle:         object = None            # AgentCycle | None — for live head reads


class Runtime:
    def __init__(self, config: Config) -> None:
        self.config = config
        
        # Shared DB for writing inbound nodes. 
        # AgentCycle will open its own connection for reading/inference.
        workspace = Path(config.workspace.path).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        # Internal data dir — agent.db, users.db, memory graph. Kept separate
        # from workspace so the agent's own filesystem tools never see it.
        data_path = Path(config.data.path).expanduser().resolve()
        data_path.mkdir(parents=True, exist_ok=True)
        self.data_path = data_path
        self.db = ConversationDB(data_path / "agent.db")

        self.commands = CommandRegistry()
        self.module_registry = ModuleRegistry()
        self.users = UserStore(data_path)

        # Tool discovery — one ToolVectorStore for the process lifetime,
        # shared by every AgentCycle's ToolCallHandler (see agent.py). The
        # tool registry itself is rebuilt fresh each turn, but embeddings are
        # content-hash cached here across turns and restarts. Embedders are
        # built lazily per model name (see get_tool_embedder below) since
        # tools.passive and tools.search may name different embedding
        # models, or neither — no sense connecting one that's never used.
        from TinyCTX.tool_handling.vector_store import ToolVectorStore
        self.tool_vector_store = ToolVectorStore(data_path / "tools_vector_cache.db")
        self._tool_embedders: dict[str, object] = {}  # model name -> ai.Embedder

        # Concurrency Management
        max_workers = getattr(config, "max_workers", 8)
        self._semaphore = asyncio.Semaphore(max_workers)
        self._active: int = 0
        self._tasks: set[asyncio.Task] = set()
        self._abort_events: dict[str, asyncio.Event] = {}

        # Concurrent Forks — see docs/PLAN.md. In-memory, per-process; dies
        # with a restart (correct — abandoned branches are just unreferenced
        # history). _settled is session_key -> node_id, the single node new
        # inbound messages attach to (§3.2). One lock per session serialises
        # the start/finish transitions only — runs execute freely (§6).
        self._runs: dict[str, Run] = {}
        self._settled: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

        # Platform renderers — see register_platform_handler / deliver below.
        self._platform_handlers: dict[str, Callable[[str, object], Awaitable[None]]] = {}

    async def start(self) -> None:
        self._register_user_commands()
        self.module_registry.load_modules(self)
        logger.info("Runtime started")

    def get_tool_embedder(self, model_name: str):
        """
        Return a cached ai.Embedder for `model_name` (tools.passive.embedding_model
        or tools.search.embedding_model), building it on first request. Returns
        None for an empty model_name, or if the name doesn't resolve to a usable
        'kind: embedding' models: entry — callers (agent.py) treat None as "fall
        back to BM25-only", same graceful-degrade contract modules/rag uses.

        Cached per model name rather than built once in __init__: tools.passive
        and tools.search may name different models (or one/both may be unset),
        so nothing is connected until something actually asks for it.
        """
        model_name = (model_name or "").strip()
        if not model_name:
            return None
        if model_name in self._tool_embedders:
            return self._tool_embedders[model_name]
        try:
            from TinyCTX.ai import Embedder
            emb_cfg = self.config.get_embedding_model(model_name)
            embedder = Embedder.from_config(emb_cfg)
        except (KeyError, ValueError, AttributeError) as exc:
            logger.warning(
                "[runtime] tool embedding_model '%s' not usable (%s) — BM25 only",
                model_name, exc,
            )
            embedder = None
        self._tool_embedders[model_name] = embedder
        return embedder

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
    # Platform delivery — render events to a destination outside a live
    # bridge-owned reply_queue drain loop (e.g. a cron job's output).
    # ------------------------------------------------------------------

    def register_platform_handler(
        self,
        platform: str,
        handler: Callable[[str, object], Awaitable[None]],
    ) -> None:
        """
        Register a renderer for `platform`: an async callable
        `(destination, event) -> None` that renders one AgentEvent to
        `destination` (a platform-specific address — e.g. a Discord
        cursor_key or a Telegram chat_key).

        Bridges call this once at startup with the same render_event
        function their own turn-handling loop uses, so any caller that
        isn't a live bridge turn (cron, and any future non-interactive
        trigger source) can deliver output through the identical
        rendering path a live user turn would use — same message
        chunking, same file-upload handling, same error formatting.

        Overwrites any previously registered handler for `platform`.
        """
        self._platform_handlers[platform] = handler
        logger.info("[runtime] platform handler registered for %r", platform)

    async def deliver(self, platform: str, destination: str, event: object) -> bool:
        """
        Render one AgentEvent to `destination` via the handler registered
        for `platform`. Returns False (and logs) if no handler is
        registered, or if the handler itself raises — a delivery failure
        must never propagate up and abort the caller's larger loop (e.g.
        a cron tick processing several due jobs).
        """
        handler = self._platform_handlers.get(platform)
        if handler is None:
            logger.warning(
                "[runtime] deliver: no platform handler registered for %r — dropping event", platform
            )
            return False
        try:
            await handler(destination, event)
            return True
        except Exception:
            logger.exception(
                "[runtime] deliver: handler for %r raised while rendering to %r", platform, destination
            )
            return False

    # ------------------------------------------------------------------
    # Entry Point: push()
    # ------------------------------------------------------------------

    async def push(
        self,
        msg: InboundMessage,
        reply_queue: asyncio.Queue | None = None,
        *,
        session_key: str | None = None,
        parent: Run | None = None,
    ) -> str:
        """
        Accepts InboundMessage, persists to DB, and triggers AgentCycle if needed.
        Always returns the new user node id.
        If reply_queue is provided and msg.trigger is True, events are written into
        it as they arrive. A None sentinel is put when the turn is complete.

        Concurrent Forks (docs/PLAN.md §3.2, R1): the new node always attaches to
        session_key's settled_tail, not to msg.tail_node_id directly. msg.tail_node_id
        is only used to seed settled_tail the first time this session_key is seen.

        settled_tail advances here only for passive (non-trigger) messages, which are
        plain linear continuation. A *triggering* message deliberately leaves it where
        it was: §3.2 defines a fork as "attach to settled_tail while settled_tail has
        not advanced past the running run's root", so advancing to the new user node
        would make the next concurrent message a child of this run's root rather than
        its sibling. finish_run() is the only thing that advances settled_tail past a
        run (R2).

        session_key scopes the roster/fan-out/nudges (§10). Callers that don't pass
        one get session_key defaulted to msg.tail_node_id — a value that is
        caller-computed and differs on essentially every call, so each such push() is
        its own one-off "session": linear attach, empty roster, no fan-out, invisible
        to real sessions. This is how internal cycles (modules/heartbeat with
        Platform.CRON, modules/cron, the memory librarian) stay out of user-facing
        rosters — §10.2's concern, handled by not minting a shared key rather than by
        a per-run flag.

        parent, when given (spawn_fork — §9.1), makes the new run inherit the
        parent run's session_key rather than minting one from session_key/msg (§10.2).
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

        # build_content_blocks returns list[dict] when attachments are inlined
        # (e.g. small images within the inline_max_bytes threshold), or a plain
        # str when every attachment exceeded the threshold and was written as a
        # reference note instead (the agent reads those files via filesystem tools).
        # Normalise to str for DB storage in both cases.
        content_str = json.dumps(content, ensure_ascii=False) if isinstance(content, list) else str(content)

        # 2. Write User Node to DB
        if msg.suppress_attribution:
            # Deliberate — see InboundMessage.suppress_attribution and
            # context.py's NO_ATTRIBUTION_SENTINEL. msg.author is still the
            # real caller (used below via _spawn_task for permission checks);
            # only the DB node's author_id — and therefore the 【label】:
            # prefix Context.assemble() would otherwise add — is affected.
            from TinyCTX.context import NO_ATTRIBUTION_SENTINEL
            author_id = NO_ATTRIBUTION_SENTINEL
        else:
            author_id = msg.author.username
            if not author_id:
                # author_id being empty/None means the prefix will be silently skipped
                # in context assembly, making multi-user attribution invisible to the LLM.
                logger.error(
                    "[push] author_id is empty for inbound message (tail=%s, platform=%s, user_id=%s). "
                    "User node will be written without author_id — prefix will be missing in LLM context.",
                    msg.tail_node_id,
                    msg.env.platform,
                    getattr(msg.author, 'user_id', '<unknown>'),
                )
        effective_session_key = parent.session_key if parent is not None else (session_key or msg.tail_node_id)

        # session_key IS the bridge's cursor_key for real (non-internal) turns
        # — e.g. Discord's handle_turn calls push(msg, session_key=cursor_key).
        # Internal callers (cron, heartbeat) never pass session_key, so it
        # defaults to msg.tail_node_id — a one-off value, correctly excluded
        # below so it's never mistaken for a real channel address.
        cursor_key = session_key if (parent is None and session_key) else None
        state_delta = self._compute_state_delta(msg, cursor_key)

        # --- R1 attach + §6 race fix: resolve settled_tail, write the node,
        # and (if triggering) register the Run, all under one session lock. ---
        lock = self._session_lock(effective_session_key)
        async with lock:
            attach_to = self._settled.setdefault(effective_session_key, msg.tail_node_id)
            user_node = self.db.add_node(
                parent_id=attach_to,
                role="user",
                content=content_str,
                author_id=author_id or None,
                state_delta=json.dumps(state_delta) if state_delta else None,
            )
            new_tail_id = user_node.id

            if not msg.trigger:
                # Passive message: linear continuation, so the attach point moves.
                self._settled[effective_session_key] = new_tail_id
                return new_tail_id

            # Triggering message: settled_tail stays put until finish_run (R2),
            # so a concurrent message forks off the same parent. See docstring.
            run = Run(
                id=str(uuid.uuid4()),
                session_key=effective_session_key,
                intent=(msg.text or "")[:200],
                root_node_id=new_tail_id,
            )
            self._runs[run.id] = run

        await self._spawn_task(run, msg.author, reply_queue)
        return new_tail_id

    async def _spawn_task(self, run: Run, caller, reply_queue: asyncio.Queue | None = None) -> None:
        """
        Launch the asyncio task that drives run through AgentCycle.

        There is deliberately no admission check here. _process() acquires
        self._semaphore, which already queues: over-capacity runs wait for a slot
        rather than being dropped. max_workers caps *concurrent execution*, not
        how much work may be accepted.
        """
        abort_ev = self._get_abort_event(run.root_node_id)
        task = asyncio.create_task(
            self._process(run, caller, abort_ev, reply_queue),
            name=f"cycle:{run.id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Concurrent Forks — Run lifecycle (docs/PLAN.md §3, §6, §7)
    # ------------------------------------------------------------------

    def _session_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    def seed_session(self, session_key: str, node_id: str) -> None:
        """
        Force session_key's settled_tail to node_id — used by bridge-side
        session bootstrapping/reset flows (e.g. /reset, thread-fork-point
        resolution) that need to establish or correct the attach point
        Runtime uses for this process's lifetime.
        """
        self._settled[session_key] = node_id

    def runs_in_session(self, session_key: str) -> list[Run]:
        """Runs scoped to session_key — for the roster provider and fan-out.
        Never process-global (§10.1)."""
        return [r for r in self._runs.values() if r.session_key == session_key]

    async def start_run(
        self,
        session_key: str,
        prompt: str,
        branch_from: str,
        caller,
        *,
        parent: Run | None = None,
    ) -> Run:
        """
        Write the run's root node as a fresh branch off branch_from, register
        the Run, and launch it — all under session_key's lock (§6). Used by
        spawn_fork (§9.1), which doesn't go through push()'s inbound-message path.

        The node write happens inside the lock rather than at the call site so
        that every start transition is serialised against finish_run's fan-out,
        exactly as push()'s is.

        When parent is given, the run inherits parent's session_key so spawned
        forks are visible to their siblings and spawner (§10.2).
        """
        if parent is not None:
            session_key = parent.session_key
        lock = self._session_lock(session_key)
        async with lock:
            branch_node = self.db.add_node(
                parent_id=branch_from,
                role="user",
                content=prompt,
                author_id=getattr(caller, "username", None) or None,
            )
            run = Run(
                id=str(uuid.uuid4()),
                session_key=session_key,
                intent=(prompt or "")[:200],
                root_node_id=branch_node.id,
            )
            self._runs[run.id] = run
        await self._spawn_task(run, caller)
        return run

    async def finish_run(self, run: Run, final_text: str, head_node_id: str) -> None:
        """
        Advance settled_tail to the last finisher and fan the digest out to
        every peer still running (R2). Under the session lock (§6): a peer
        starting concurrently either observes the advanced tail or is visible
        to this fan-out — never neither.
        """
        lock = self._session_lock(run.session_key)
        async with lock:
            run.status = "done"
            self._settled[run.session_key] = head_node_id
            notice = self._render_digest(run, final_text)
            for peer in self._runs.values():
                if peer.id == run.id or peer.session_key != run.session_key:
                    continue
                if peer.status != "running":
                    continue
                peer.inbox.put_nowait(Exogenous(kind="fork_finished", role="user", content=notice))
            self._runs.pop(run.id, None)

    async def _abandon_run(self, run: Run) -> None:
        """
        Failure/abort counterpart to finish_run: advance settled_tail past a
        run that died without producing a digest, so the session keeps moving
        instead of stranding every later message on the dead run's parent.
        No fan-out — nothing meaningful happened to tell peers about.
        """
        cycle = run.cycle
        head = getattr(getattr(cycle, "context", None), "tail_node_id", None) or run.root_node_id
        lock = self._session_lock(run.session_key)
        async with lock:
            self._settled[run.session_key] = head

    def nudge(self, target_id: str, from_run: Run, message: str) -> bool:
        """
        Advisory, one-way, same-session-only message from one running fork to
        another (§11). Returns False if the target isn't a running peer in the
        same session — nudging a finished/unknown/foreign run is a benign no-op.
        """
        target = self._runs.get(target_id)
        if target is None or target.status != "running":
            return False
        if target.session_key != from_run.session_key:
            return False
        content = (
            f"[nudge from fork {from_run.id[:8]} — {from_run.intent!r}]\n"
            f"{message}\n\n"
            "(Advisory, from a peer fork. Not from the user. You decide whether to comply.)"
        )
        target.inbox.put_nowait(Exogenous(kind="nudge", role="user", content=content))
        return True

    @staticmethod
    def _render_digest(run: Run, final_text: str) -> str:
        """
        Render a finished run's completion digest — intent + final text only,
        no tools, no thinking (§1). Truncated so one long fork can't blow the
        context budget of every peer it fans out to (open decision §12.3).
        Delivered into a peer's inbox as user-role with an explicit wrapper —
        unambiguous to the peer that this happened elsewhere (§12.1).
        """
        text = (final_text or "").strip()
        if len(text) > _DIGEST_MAX_CHARS:
            text = text[:_DIGEST_MAX_CHARS] + "...[truncated]"
        return f"[fork {run.id[:8]} finished — intent: {run.intent!r}]\n{text}"

    # ------------------------------------------------------------------
    # Processing Logic
    # ------------------------------------------------------------------

    async def _process(self, run: Run, caller, abort_event: asyncio.Event, reply_queue: asyncio.Queue | None = None) -> None:
        from TinyCTX.agent import AgentCycle

        async with self._semaphore:
            self._active += 1
            try:
                agent = AgentCycle(self.config, self.module_registry)
                run.cycle = agent
                logger.debug("[runtime] cycle starting for run %s (node %s)", run.id, run.root_node_id)

                async for event in agent.run(run, caller, abort_event, runtime=self):
                    if reply_queue is not None:
                        await reply_queue.put(event)

                logger.debug("[runtime] cycle complete for run %s", run.id)
            except Exception as exc:
                logger.exception("Cycle failed for run %s", run.id)
                run.status = "failed"
                # Surface it. Without this the bridge drains an empty queue,
                # hits the sentinel, and sends nothing — a crashed cycle is
                # indistinguishable from the agent choosing not to reply.
                if reply_queue is not None:
                    await reply_queue.put(AgentError(
                        message=f"Cycle failed: {type(exc).__name__}: {exc}",
                        trace_id=run.id,
                        reply_to_message_id="synthetic",
                        tail_node_id=run.root_node_id,
                    ))
            finally:
                self._active -= 1
                self._abort_events.pop(run.root_node_id, None)
                # Belt-and-suspenders cleanup: AgentCycle.run() normally calls
                # finish_run() itself on the success path, which pops the run.
                # Abort / LLM-error paths return early without a digest to fan
                # out (nothing meaningful happened) — make sure the run doesn't
                # linger in _runs forever either way.
                if run.status == "running":
                    run.status = "aborted" if abort_event.is_set() else "failed"
                if run.status != "done":
                    # finish_run (R2) never ran, so settled_tail is still
                    # parked at this run's parent. Leaving it there makes
                    # every subsequent message fork off the same stale node
                    # forever — the session stops advancing. Release it to
                    # the furthest node this run actually wrote.
                    await self._abandon_run(run)
                self._runs.pop(run.id, None)
                if reply_queue is not None:
                    await reply_queue.put(None)  # sentinel: turn complete

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _compute_state_delta(self, msg: InboundMessage, cursor_key: str | None = None) -> dict:
        prior_state, _ = self.db.load_session_state(msg.tail_node_id)
        delta = {}
        mapping = {
            "platform":     msg.env.platform.value,
            "agent_name":   msg.env.agent_name,
            "server_name":  msg.env.server_name,
            "channel_name": msg.env.channel_name,
            "author_id":    msg.author.username,
            # Stable channel/chat address (e.g. "group:<id>", "tg:<id>") —
            # see push()'s cursor_key derivation above. Recorded so
            # non-live callers (cron's add_cron tool) can recover "the
            # channel this turn is happening in" without importing any
            # bridge module — read back via db.get_state(tail, "cursor_key").
            "cursor_key":   cursor_key,
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