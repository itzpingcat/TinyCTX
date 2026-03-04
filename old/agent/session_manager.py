from __future__ import annotations
import importlib
import json
import pkgutil
import sys
from pathlib import Path

import yaml

from agent.registry import Registry
from agent.session import Session
from utils.ai import LLM
from config import cfg
# ---------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------

class SessionManager:
    """
    Entry point for the whole system.

    Responsibilities:
      - Load config.yaml
      - Instantiate the shared Registry
      - Discover modules; call register_global() once into Registry
      - Spawn, load, and destroy sessions
      - Persist sessions under sessions/{name}/{version}.json

    Session naming:
      Sessions have a name (e.g. "discord") and an integer version.
      Full ID is "{name}_{version}".
      Bridges load sessions by name (gets latest version) or full ID.
      Resetting a session bumps the version, preserving history.

    Extension loading:
      register_global(registry, config) — called once at startup
      register(context, config)         — called per session on create/load
    """
    global cfg
    def __init__(self, config_path: str = "config.yaml"):
        self.sessions_dir = Path(cfg.get("sessions_dir", "./sessions"))
        self.registry     = Registry()

        # (name, main_mod, ext_config) — used to re-register into each session
        self._extension_mods: list[tuple] = []

        # id -> Session
        self._sessions: dict[str, Session] = {}

        self._discover_modules(cfg.get("modules_path", "./modules"))

    # -----------------------------------------------------------------
    # modules
    # -----------------------------------------------------------------

    def _discover_modules(self, modules_path: str):
        path = Path(modules_path).resolve()
        if not path.exists():
            print(f"[manager] modules path '{path}' not found, skipping")
            return

        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))

        pkg_name = path.name
        disabled = set(cfg.get("disabled_modules", []))
        ext_cfgs = cfg.get("modules", {})

        for _, name, is_pkg in pkgutil.iter_modules([str(path)]):
            if not is_pkg or name in disabled:
                if name in disabled:
                    print(f"[manager] '{name}' disabled")
                continue

            try:
                init_mod = importlib.import_module(f"{pkg_name}.{name}")
            except ImportError as e:
                print(f"[manager] failed to import {pkg_name}.{name}: {e}")
                continue

            meta       = getattr(init_mod, "EXTENSION_META", {})
            ext_config = {**meta.get("default_config", {}), **ext_cfgs.get(name, {})}

            try:
                main_mod = importlib.import_module(f"{pkg_name}.{name}.__main__")
            except ImportError as e:
                print(f"[manager] failed to import {pkg_name}.{name}.__main__: {e}")
                continue

            if not hasattr(main_mod, "register"):
                print(f"[manager] '{name}' missing register(), skipping")
                continue

            # global registration (tools, file handlers) — once only
            if hasattr(main_mod, "register_global"):
                try:
                    main_mod.register_global(self.registry, ext_config)
                except Exception as e:
                    print(f"[manager] '{name}' register_global() failed: {e}")

            self._extension_mods.append((name, main_mod, ext_config))
            print(f"[manager] loaded '{name}' v{meta.get('version', '?')}")

    def _register_modules_into(self, session: Session):
        """Call register(context, config) for every extension into a session."""
        for name, main_mod, ext_config in self._extension_mods:
            try:
                main_mod.register(session.context, ext_config)
            except Exception as e:
                print(f"[manager] '{name}' register() failed for '{session.id}': {e}")

    # -----------------------------------------------------------------
    # Session lifecycle
    # -----------------------------------------------------------------

    def create_session(self, name: str) -> Session:
        """
        Create a new session with the next available version for that name.

        sessions/discord/1.json -> next is 2, etc.
        First session for a name is version 1.
        """
        version = self._next_version(name)
        session = self._build_session(name, version)
        session._save()
        self._sessions[session.id] = session
        print(f"[manager] created session '{session.id}'")
        return session

    def load_session(self, name: str, version: int | None = None) -> Session:
        """
        Load a saved session by name and optional version.
        If version is omitted, loads the latest version.

        Raises FileNotFoundError if no saved session exists.
        """
        if version is None:
            version = self._latest_version(name)
            if version is None:
                raise FileNotFoundError(f"No saved sessions found for '{name}'")

        sid = f"{name}_{version}"

        # return already-loaded instance if available
        if sid in self._sessions:
            return self._sessions[sid]

        path = self.sessions_dir / name / f"{version}.json"
        if not path.exists():
            raise FileNotFoundError(f"Session file not found: {path}")

        data    = json.loads(path.read_text())
        session = self._build_session(name, version)
        session._load(data)

        self._sessions[sid] = session
        print(f"[manager] loaded session '{sid}'")
        return session

    def reset_session(self, name: str) -> Session:
        """
        Create a new version for an existing session name.
        Old versions remain on disk and can be loaded at any time.
        """
        return self.create_session(name)

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def unload_session(self, session_id: str):
        """Remove session from memory. Data stays on disk."""
        self._sessions.pop(session_id, None)
        print(f"[manager] unloaded session '{session_id}'")

    def list_sessions(self) -> dict[str, list[int]]:
        """
        Return all saved sessions on disk.
        {"discord": [1, 2, 3], "heartbeat": [1]}
        """
        result: dict[str, list[int]] = {}
        if not self.sessions_dir.exists():
            return result
        for name_dir in sorted(self.sessions_dir.iterdir()):
            if not name_dir.is_dir():
                continue
            versions = sorted(
                int(f.stem) for f in name_dir.glob("*.json") if f.stem.isdigit()
            )
            if versions:
                result[name_dir.name] = versions
        return result

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _build_session(self, name: str, version: int) -> Session:
        """Construct a Session and register all modules into its context."""
        llm_cfg = cfg.llm
        llm = LLM(
            model      = llm_cfg.get("model",       "gpt-4o"),
            base_url   = llm_cfg.get("base_url",    "https://api.openai.com"),
            api_key    = llm_cfg.get("api_key"),
            max_tokens = llm_cfg.get("max_tokens",  2048),
            temperature= llm_cfg.get("temperature", 0.7),
            timeout    = llm_cfg.get("timeout",     60),
        )
        session = Session(
            name         = name,
            version      = version,
            llm          = llm,
            registry     = self.registry,
            config       = cfg,
            sessions_dir = self.sessions_dir,
        )
        self._register_modules_into(session)
        return session

    def _next_version(self, name: str) -> int:
        latest = self._latest_version(name)
        return 1 if latest is None else latest + 1

    def _latest_version(self, name: str) -> int | None:
        name_dir = self.sessions_dir / name
        if not name_dir.exists():
            return None
        versions = [
            int(f.stem) for f in name_dir.glob("*.json") if f.stem.isdigit()
        ]
        return max(versions) if versions else None