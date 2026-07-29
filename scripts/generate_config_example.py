#!/usr/bin/env python3
"""
generate_config_example.py — Autogenerate example.config.yaml from the
actual config schema, so documented defaults never drift from the code.

Config defaults live in two kinds of places:
  1. TinyCTX/config/__main__.py — the core dataclasses (Config, ModelConfig,
     GatewayConfig, WorkspaceConfig, etc.) that define every top-level
     config.yaml key. Introspected here via dataclasses.fields(), never
     instantiated (some of these classes read environment variables in
     __post_init__, which we don't want leaking into the generated example).
  2. Each module's EXTENSION_META["default_config"] dict, in
     TinyCTX/modules/<name>/__init__.py (and TinyCTX/custom_modules/<name>/
     __main__.py) — per-module settings read at runtime via
     agent.config.extra.get("<name>", {}).

Both are parsed statically with ast (no imports of the modules themselves),
so this script never needs the project's runtime dependencies installed
and never executes third-party module code.

Known gap: a handful of modules read extra keys via inline
`cfg.get("key", default)` calls in __main__.py that were never added to
default_config (e.g. filesystem's `read_only_paths`). This script does a
best-effort scan for that pattern too, but only within each module's own
__main__.py — not helper submodules it imports (e.g. modules/memory/
deduper.py, modules/memory/flaggers/*.py). Cross-check against those by
hand if you're adding config for a module with sub-files.

custom_modules/ is gitignored (user-local plugins), so it's skipped by
default — pass --custom to also scan it.

Usage:
    python scripts/generate_config_example.py
    python scripts/generate_config_example.py --custom
    python scripts/generate_config_example.py --output example.config.yaml
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = REPO_ROOT / "TinyCTX" / "modules"
CUSTOM_MODULES_DIR = REPO_ROOT / "TinyCTX" / "custom_modules"


# --------------------------------------------------------------------------- core schema
def _dataclass_field_defaults(cls) -> dict:
    """
    {field_name: default} for a dataclass, recursing into nested dataclasses
    without ever instantiating them (so __post_init__ never runs). Fields
    with no static default (required, e.g. ModelConfig.model) come back None.
    """
    out = {}
    for f in dataclasses.fields(cls):
        if not f.init:
            continue
        if f.default is not dataclasses.MISSING:
            out[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            factory = f.default_factory
            out[f.name] = _dataclass_field_defaults(factory) if dataclasses.is_dataclass(factory) else factory()
        else:
            out[f.name] = None
    return out


def _to_jsonable(value):
    if dataclasses.is_dataclass(value):
        cls = value if isinstance(value, type) else type(value)
        return {k: _to_jsonable(v) for k, v in _dataclass_field_defaults(cls).items()} if isinstance(value, type) else \
            {f.name: _to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value) if f.init}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _core_schema() -> dict:
    from TinyCTX.config.__main__ import Config, LLMRoutingConfig, ModelConfig

    schema = _to_jsonable(_dataclass_field_defaults(Config))

    # `extra` is config.py's catch-all for unknown top-level keys — it's
    # exactly where module sections (heartbeat:, rag:, ...) get merged back
    # in below, so it has no place in the emitted schema itself.
    schema.pop("extra", None)

    # `models` and `llm` are required (no dataclass default) — show one
    # filled-in example model and llm's real defaults instead of null.
    model_defaults = _to_jsonable(_dataclass_field_defaults(ModelConfig))
    model_defaults["model"] = "REPLACE_ME (e.g. claude-sonnet-4-5)"
    model_defaults["base_url"] = "REPLACE_ME (e.g. https://api.anthropic.com/v1)"
    schema["models"] = {"main": model_defaults}
    schema["llm"] = _to_jsonable(_dataclass_field_defaults(LLMRoutingConfig))

    # workspace.path / data.path: the bare dataclass default is ~/.tinyctx,
    # which only applies when Config is built directly, bypassing load().
    # load()'s real default is "<the config file's own directory>/workspace"
    # (or /data) — shown here as a portable relative path, not whatever
    # $HOME happens to resolve to on the machine that ran this script.
    schema["workspace"]["path"] = "workspace"
    schema["data"]["path"] = "data"

    return schema


# --------------------------------------------------------------------------- module defaults
def _literal_dict(node: ast.AST) -> dict | None:
    try:
        val = ast.literal_eval(node)
    except Exception:
        return None
    return val if isinstance(val, dict) else None


def _find_extension_meta(tree: ast.Module) -> dict | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "EXTENSION_META" for t in node.targets):
            return _literal_dict(node.value)
    return None


def _is_extra_get(call: ast.Call) -> bool:
    """<...>.extra.get("name", ...) or <...>._raw.get("name", ...)."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "get"):
        return False
    base = call.func.value
    return isinstance(base, ast.Attribute) and base.attr in ("extra", "_raw")


def _is_default_config_get(call: ast.Call) -> bool:
    """EXTENSION_META.get("default_config", ...)."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "get"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "EXTENSION_META"
    )


def _config_var_names(tree: ast.Module) -> set[str]:
    """
    Variable names that, at some point in the file, hold this module's own
    config dict: copied from EXTENSION_META["default_config"], or read
    straight from agent.config.extra.get(<name>, ...) / ._raw.get(...).
    Handles the `dict(...)`, `.copy()`, and `X or {}` wrapping seen in
    practice across modules.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and value.values:
            value = value.values[0]
        call = value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "dict" and call.args:
            call = call.args[0]
        if isinstance(call, ast.Attribute) and call.attr == "copy":
            call = call.value
        if not isinstance(call, ast.Call):
            continue
        if _is_extra_get(call) or _is_default_config_get(call):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _scan_inline_gets(tree: ast.Module, var_names: set[str]) -> dict:
    """Best-effort: X.get("key", <literal default>) for X in var_names."""
    found = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
            continue
        base = node.func.value
        if not (isinstance(base, ast.Name) and base.id in var_names):
            continue
        if not node.args or not (isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            continue
        key = node.args[0].value
        default_node = node.args[1] if len(node.args) > 1 else ast.Constant(value=None)
        try:
            default = ast.literal_eval(default_node)
        except Exception:
            continue
        found.setdefault(key, default)
    return found


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return None


def _module_defaults(module_dirs: list[Path]) -> dict:
    """Scan the given module directories for EXTENSION_META; return {module_name: merged_default_config}."""
    out: dict[str, dict] = {}
    for base in module_dirs:
        if not base.exists():
            continue
        for entry in sorted(p for p in base.iterdir() if p.is_dir()):
            init_path = entry / "__init__.py"
            main_path = entry / "__main__.py"
            meta_source = init_path if init_path.exists() else main_path
            if not meta_source.exists():
                continue
            tree = _parse(meta_source)
            meta = _find_extension_meta(tree) if tree else None
            if meta is None and meta_source != main_path:
                tree = _parse(main_path)
                meta = _find_extension_meta(tree) if tree else None
            if meta is None:
                continue

            name = meta.get("name", entry.name)
            defaults = dict(meta.get("default_config", {}) or {})

            main_tree = _parse(main_path)
            if main_tree is not None:
                var_names = _config_var_names(main_tree)
                for key, default in _scan_inline_gets(main_tree, var_names).items():
                    defaults.setdefault(key, default)

            if defaults:
                if name in out:
                    print(f"[warn] duplicate module config name '{name}' ({entry}) — keeping first seen", file=sys.stderr)
                    continue
                out[name] = defaults
    return out


# --------------------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=str(REPO_ROOT / "example.config.yaml"), help="Output path (default: repo_root/example.config.yaml)")
    parser.add_argument("--custom", action="store_true", help="Also scan TinyCTX/custom_modules/ (gitignored, user-local plugins — skipped by default)")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT))

    module_dirs = [MODULES_DIR] + ([CUSTOM_MODULES_DIR] if args.custom else [])

    core = _core_schema()
    modules = _module_defaults(module_dirs)

    collisions = set(core) & set(modules)
    if collisions:
        print(f"[warn] module name(s) collide with core config keys, core wins: {sorted(collisions)}", file=sys.stderr)
        for name in collisions:
            modules.pop(name)

    merged = {**core, **modules}

    out_path = Path(args.output)
    import yaml
    out_path.write_text(yaml.dump(merged, sort_keys=True, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    print(f"Wrote {out_path} ({len(merged)} top-level keys, {len(modules)} module section(s))")


if __name__ == "__main__":
    main()
