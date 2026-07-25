"""
utils/instance.py — shared instance-directory resolution.

An "instance" is a directory containing config.yaml, workspace/, and
data/. All CLI commands (start/stop/status/launch/onboard) resolve the
same instance directory the same way, so multiple agents can run side by
side as long as each has its own instance dir.

Resolution order for the instance directory:
  1. Explicit path (--dir flag), if given
  2. If the current directory is named .tinyctx, or is nested inside a
     directory named .tinyctx (e.g. running from inside
     <instance>/workspace/skills/foo), that ancestor is the instance.
     Only matches a directory literally named .tinyctx — never an
     arbitrary ancestor — so this can't silently attach to an unrelated
     project higher up the tree.
  3. .tinyctx/ living inside the current directory (exact child match)
  4. Fallback: ~/.tinyctx

This does not auto-create the directory. Callers should error out and
point the user at `tinyctx onboard` if it doesn't exist yet.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def load_instance_env(instance_dir: Path) -> None:
    """
    Load `<instance_dir>/.env` into the process environment, if present.

    Values in the file override anything already set in the environment
    (e.g. a stale global DISCORD_BOT_TOKEN exported in the shell profile) —
    this instance's .env is meant to be the source of truth for its secrets.
    No-op if the file doesn't exist.
    """
    env_path = instance_dir / ".env"
    if not env_path.is_file():
        return
    from dotenv import load_dotenv
    load_dotenv(env_path, override=True)


def resolve_instance_dir(explicit: str | None = None) -> Path:
    """Resolve the instance directory per the order documented above."""
    if explicit:
        return Path(explicit).expanduser().resolve()

    cwd = Path.cwd().resolve()

    # Running from inside an instance dir itself (or a subdirectory of one) —
    # walk up looking for the nearest ancestor literally named .tinyctx.
    for candidate in (cwd, *cwd.parents):
        if candidate.name == ".tinyctx":
            return candidate

    # Running from a directory that *contains* a .tinyctx instance as a child.
    cwd_candidate = cwd / ".tinyctx"
    if cwd_candidate.is_dir():
        return cwd_candidate

    return (Path.home() / ".tinyctx").resolve()


def config_path_for(instance_dir: Path) -> Path:
    """config.yaml lives directly inside the instance directory."""
    return instance_dir / "config.yaml"


def project_name_for(instance_dir: Path) -> str:
    """
    Stable, short `docker compose -p` project name derived from the
    instance directory's absolute path. Guarantees two different instance
    dirs never collide on container/network names, without requiring the
    user to pick a name. Used for -p and container_name, neither of which
    has a tight OS-level length limit.
    """
    h = hashlib.sha256(str(instance_dir).encode("utf-8")).hexdigest()[:10]
    return f"tinyctx-{h}"


def bridge_tag_for(instance_dir: Path) -> str:
    """
    Short (6 hex char) tag for Docker bridge interface names
    (com.docker.network.bridge.name in compose.yaml).

    Linux caps network interface names at 15 chars (IFNAMSIZ). Bridge names
    are built as br_<tag> or br_<tag>_ab / br_<tag>_sb (longest suffix is 3
    chars + underscore), so the tag itself must stay short: 'br_' (3) +
    6 hex chars + '_ab' (3) = 12, safely under the limit. project_name_for's
    output is too long to use here directly.
    """
    return hashlib.sha256(str(instance_dir).encode("utf-8")).hexdigest()[:6]


def compose_env(instance_dir: Path, port: int | None = None) -> dict[str, str]:
    """
    Env vars to inject into the `docker compose` subprocess call so the
    (repo-root, shared) compose.yaml binds to this instance's config.yaml,
    workspace/, and data/ (and, optionally, a specific host port), without
    needing a .env file next to the compose file.

    TINYCTX_INSTANCE names the container(s) (Docker allows long names).
    TINYCTX_TAG is a short hash used only for bridge interface names, which
    Linux caps at 15 chars — see bridge_tag_for().
    """
    env: dict[str, str] = {
        "TINYCTX_CONFIG_FILE": str(instance_dir / "config.yaml"),
        "TINYCTX_WORKSPACE":   str(instance_dir / "workspace"),
        "TINYCTX_DATA":        str(instance_dir / "data"),
        "TINYCTX_INSTANCE":    project_name_for(instance_dir),
        "TINYCTX_TAG":         bridge_tag_for(instance_dir),
    }
    if port is not None:
        env["TINYCTX_PORT"] = str(port)
    return env
