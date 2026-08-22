"""
modules/comfyui/__main__.py

generate_image_comfyui — runs an admin-provided ComfyUI workflow in-process
(no subprocess). Delivers the generated image directly to the user via
present().

Workflows are JSON files dropped by the admin into the instance's read-only
extra-config directory, resolved via utils/instance.py::runtime_config_dir():

    <instance>/config/comfyui/<name>.json

The agent picks a workflow by name (bare filename, no extension). Available
names are discovered once at startup and baked into the tool's docstring.

Uses the raw ComfyUI **API v1** surface (POST /prompt, GET /history/{id},
GET /view). This talks directly to stock ComfyUI (default 127.0.0.1:8188) —
no comfy-api-proxy or Comfy Cloud v2 job surface required.

Config (read from the top-level `comfyui:` key in config.yaml, via
agent.config.extra — same mechanism as the `mcp:` block):

  comfyui:
    host: 127.0.0.1        # ComfyUI host
    port: 8188              # ComfyUI port (stock ComfyUI default)
    api_key: null           # optional Bearer token (reverse-proxied setups)
    unload_after: true      # POSTs /free {unload_models, free_memory} when done
    timeout: 300             # seconds to wait for the job to finish
    safety_filter:
      enabled: true
      min_score: 0.2
      hard_blocked_labels:   # image is WITHHELD from LLM if any of these are detected
        - FEMALE_GENITALIA_EXPOSED
        - MALE_GENITALIA_EXPOSED
        - ANUS_EXPOSED
      soft_blocked_labels:   # image IS sent to LLM, but with a censor notice attached
        - FEMALE_BREAST_EXPOSED
        - BUTTOCKS_EXPOSED

Marker convention: a workflow JSON marks injectable spots with
MARKER>>name<<MARKER (as a JSON string value, even where the target field is
normally numeric — see _inject). The five supported names are:
positive-prompt, negative-prompt, seed, width, height. A workflow does not
have to use all five; if the agent passes a non-default value for one the
workflow doesn't reference, the tool call still succeeds but the returned
text carries a warning that the value was ignored.
"""
from __future__ import annotations

EXTENSION_META = {
    "name": "comfyui",
    "version": "1.0",
    "description": "generate_image_comfyui tool. Runs an admin-provided ComfyUI workflow from config/comfyui/.",
    "default_config": {
        "timeout": 300,
        "host": "127.0.0.1",
        "port": 8188,
        "api_key": "null",
        "unload_after": True,
    },
}

# The five marker names _inject knows how to substitute, and which tool
# parameter each corresponds to (for the "ignored" warning).
_MARKER_KEYS = ("positive-prompt", "negative-prompt", "seed", "width", "height")


def register_agent(agent) -> None:
    import json
    import logging
    import re
    import time
    import uuid
    from pathlib import Path

    import requests

    from TinyCTX.permissions import Permission
    from TinyCTX.utils.instance import runtime_config_dir

    logger = logging.getLogger(__name__)

    # ---------------------------------------------------------------------------
    # Paths & config
    # ---------------------------------------------------------------------------
    _module_dir    = Path(__file__).parent
    _workspace_path = Path(agent.config.workspace.path)
    _workflow_dir  = runtime_config_dir(_workspace_path) / "comfyui"
    _output_dir    = _workspace_path / "outputs" / "comfyui"

    _cfg = EXTENSION_META["default_config"].copy()
    _cfg.update(agent.config.extra.get("comfyui", {}))

    _comfy_url    = f"http://{_cfg['host']}:{_cfg['port']}"
    _timeout      = int(_cfg["timeout"])
    _unload_after = bool(_cfg["unload_after"])
    _api_key      = _cfg.get("api_key", None)
    _client_id    = str(uuid.uuid4())

    # ---------------------------------------------------------------------------
    # Workflow discovery
    # ---------------------------------------------------------------------------
    if _workflow_dir.is_dir():
        _workflow_names = sorted(p.stem for p in _workflow_dir.glob("*.json"))
    else:
        _workflow_names = []

    if not _workflow_names:
        logger.warning(
            "comfyui: no workflows found in %s — generate_image_comfyui not registered",
            _workflow_dir,
        )
        return

    logger.info("comfyui: discovered workflows: %s", _workflow_names)

    # ---------------------------------------------------------------------------
    # Safety filter
    # ---------------------------------------------------------------------------
    from .filter import apply_filter, resolve_blocked_ids

    _filter_cfg     = _cfg.get("safety_filter", {})
    _filter_enabled = bool(_filter_cfg.get("enabled", False))
    _filter_score   = float(_filter_cfg.get("min_score", 0.2))

    _hard_blocked_ids = resolve_blocked_ids(_filter_cfg.get("hard_blocked_labels", []))
    _soft_blocked_ids = resolve_blocked_ids(_filter_cfg.get("soft_blocked_labels", []))

    if _filter_enabled:
        logger.info(
            "comfyui: safety filter enabled — hard-blocking %d label(s): %s | soft-blocking %d label(s): %s",
            len(_hard_blocked_ids), list(_filter_cfg.get("hard_blocked_labels", [])),
            len(_soft_blocked_ids), list(_filter_cfg.get("soft_blocked_labels", [])),
        )
    else:
        logger.info("comfyui: safety filter disabled")

    # ---------------------------------------------------------------------------
    # Marker substitution
    # ---------------------------------------------------------------------------
    # Matches a JSON string value that is *exactly* one marker, e.g.
    # "MARKER>>width<<MARKER" — the whole field, not embedded in other text.
    # This lets a marker stand in for a non-string field (seed/width/height
    # are normally JSON ints); on an exact match we substitute the real
    # typed value instead of stringifying it. A marker embedded inside a
    # longer string (e.g. prompts) still substitutes as text via the
    # partial-match regex below.
    _EXACT_MARKER_RE  = re.compile(r'^MARKER>>([^<\n]+?)<<MARKER$')
    _PARTIAL_MARKER_RE = re.compile(r'MARKER>>([^<\n]+?)<<MARKER')

    def _inject(obj, params: dict, used: set[str]) -> object:
        """
        Recursively substitute MARKER>>name<<MARKER placeholders.

        `used` is mutated in place to record every marker name actually
        found in the workflow, so the caller can warn about params that had
        nowhere to go.
        """
        if isinstance(obj, str):
            exact = _EXACT_MARKER_RE.match(obj)
            if exact:
                name = exact.group(1).strip()
                if name in params:
                    used.add(name)
                    return params[name]
                return obj
            def _sub(m: re.Match) -> str:
                name = m.group(1).strip()
                if name in params:
                    used.add(name)
                    return str(params[name])
                return m.group(0)
            return _PARTIAL_MARKER_RE.sub(_sub, obj)
        if isinstance(obj, dict):
            return {k: _inject(v, params, used) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_inject(v, params, used) for v in obj]
        return obj

    # ---------------------------------------------------------------------------
    # ComfyUI helpers (API v1: /prompt, /history, /view, /free)
    # ---------------------------------------------------------------------------
    def _headers() -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if _api_key:
            headers["Authorization"] = f"Bearer {_api_key}"
        return headers

    def _wait_for_comfy(timeout: int = 5) -> None:
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                if requests.get(f"{_comfy_url}/system_stats", headers=_headers(), timeout=2).ok:
                    return
            except Exception as e:
                last_err = e
            time.sleep(0.5)
        raise RuntimeError(
            f"ComfyUI did not respond within {timeout}s"
            + (f": {last_err}" if last_err else "")
        )

    def _submit(workflow: dict) -> str:
        """Submit a workflow to the ComfyUI API v1 (POST /prompt)."""
        url = f"{_comfy_url}/prompt"
        payload = {"prompt": workflow, "client_id": _client_id}
        try:
            response = requests.post(url, json=payload, headers=_headers(), timeout=5)
            if response.status_code >= 400:
                # ComfyUI returns 400 with {"error": ..., "node_errors": {...}} on
                # invalid workflows — surface that instead of a bare HTTP error.
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text
                raise RuntimeError(f"ComfyUI rejected the prompt: {detail}")
            data = response.json()
            node_errors = data.get("node_errors") or {}
            if node_errors:
                raise RuntimeError(f"ComfyUI reported node errors: {node_errors}")
            return data["prompt_id"]
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to submit workflow: {e}")

    def _poll(prompt_id: str, timeout: int = _timeout) -> dict:
        """Poll GET /history/{id} until the job shows up as finished."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{_comfy_url}/history/{prompt_id}", headers=_headers(), timeout=2
                )
                response.raise_for_status()
                data = response.json()
                entry = data.get(prompt_id)
                if entry:
                    status = entry.get("status", {}) or {}
                    if status.get("completed") is not False:
                        # "completed" is only present once the job is done; an
                        # entry existing in /history at all means it finished
                        # (success or error) unless explicitly still running.
                        return entry
                time.sleep(1)
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"Failed to poll job {prompt_id}: {e}")
        raise TimeoutError(f"Job {prompt_id} did not complete within {timeout}s")

    def _download(filename: str, subfolder: str, img_type: str) -> Path:
        """Download an output image via GET /view."""
        response = requests.get(
            f"{_comfy_url}/view",
            params={"filename": filename, "subfolder": subfolder, "type": img_type},
            headers=_headers(),
            timeout=10,
            stream=True,
        )
        response.raise_for_status()
        _output_dir.mkdir(parents=True, exist_ok=True)
        out_path = _output_dir / filename
        with open(out_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return out_path

    def _load_workflow(name: str) -> dict:
        """Load a workflow JSON by bare name from _workflow_dir."""
        path = _workflow_dir / f"{name}.json"
        try:
            with open(path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Workflow file not found: {path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in workflow file: {e}")

    def _free_memory() -> None:
        """POST /free to unload models and free VRAM (v1 endpoint)."""
        try:
            requests.post(
                f"{_comfy_url}/free",
                json={"unload_models": True, "free_memory": True},
                headers=_headers(),
                timeout=5,
            )
        except Exception as e:
            logger.debug("comfyui: /free request failed (non-fatal): %s", e)

    def generate_image_comfyui(
        workflow: str,
        positive_prompt: str,
        negative_prompt: str,
        dimensions: str = "1024x1024",
        seed: int = 0,
    ) -> str:
        f"""Generate an image using a ComfyUI workflow.

        workflow: name of the workflow to run, one of {_workflow_names}.
        dimensions: "WIDTHxHEIGHT", e.g. "1024x1024". A workflow that
            doesn't support custom dimensions ignores this (see returned
            warnings).
        seed: default 0. A workflow that doesn't support seeding ignores
            this (see returned warnings).
        """
        # --- resolve workflow name -------------------------------------
        if not re.fullmatch(r"[A-Za-z0-9_-]+", workflow):
            return (
                f"Error: invalid workflow name '{workflow}'. "
                f"Available: {_workflow_names}"
            )
        if workflow not in _workflow_names:
            return f"Error: unknown workflow '{workflow}'. Available: {_workflow_names}"

        # --- parse dimensions --------------------------------------------
        m = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", dimensions, re.IGNORECASE)
        if not m:
            return f"Error: invalid dimensions '{dimensions}' — expected 'WIDTHxHEIGHT', e.g. '1024x1024'."
        width, height = int(m.group(1)), int(m.group(2))
        if width <= 0 or height <= 0:
            return f"Error: invalid dimensions '{dimensions}' — width and height must be positive."

        try:
            raw_workflow = _load_workflow(workflow)
        except Exception as e:
            return f"Error: failed to read workflow: {e}"

        params = {
            "positive-prompt": positive_prompt,
            "negative-prompt": negative_prompt,
            "seed": seed,
            "width": width,
            "height": height,
        }
        used: set[str] = set()
        prepared_workflow = _inject(raw_workflow, params, used)

        # --- warn about params this workflow can't use --------------------
        warnings: list[str] = []
        _param_defaults = {
            "negative-prompt": None,  # always meaningful if non-empty; only warn on unused+non-default below
            "seed": 0,
            "width": 1024,
            "height": 1024,
        }
        # positive/negative prompt: warn whenever supplied but unused, since
        # there's no sensible "default" prompt to compare against.
        for key in ("positive-prompt", "negative-prompt"):
            if key not in used:
                warnings.append(
                    f"[Warning: this workflow does not support {key.replace('-', '_')} — it was ignored.]"
                )
        for key in ("seed", "width", "height"):
            if key not in used and params[key] != _param_defaults[key]:
                warnings.append(
                    f"[Warning: this workflow does not support {key} — it was ignored.]"
                )

        try:
            _wait_for_comfy(timeout=5)
        except RuntimeError as e:
            return f"Error: {e}"

        logger.info("comfyui: submitting prompt (workflow=%s, timeout=%ds)", workflow, _timeout)

        try:
            prompt_id = _submit(prepared_workflow)
        except Exception as e:
            return f"Error: failed to submit prompt: {e}"

        logger.info("comfyui: job submitted: %s", prompt_id)

        try:
            history_entry = _poll(prompt_id, timeout=_timeout)
        except TimeoutError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: polling failed: {e}"

        status = (history_entry.get("status", {}) or {})
        if status.get("status_str") == "error":
            return f"Error: generation failed: {status.get('messages', status)}"

        # Collect every image reference across all node outputs.
        image_refs: list[dict] = []
        for node_output in (history_entry.get("outputs", {}) or {}).values():
            for img in node_output.get("images", []) or []:
                image_refs.append(img)

        if not image_refs:
            debug_path = None
            try:
                _output_dir.mkdir(parents=True, exist_ok=True)
                debug_path = _output_dir / f"debug_history_{prompt_id}.json"
                debug_path.write_text(json.dumps(history_entry, indent=2, default=str))
            except Exception as e:
                logger.warning("comfyui: failed to write history debug dump: %s", e)
            logger.warning(
                "comfyui: job %s completed with no image outputs. outputs keys=%s status=%s",
                prompt_id, list((history_entry.get("outputs") or {}).keys()), status,
            )
            logger.warning("comfyui: raw history entry for %s:\n%s", prompt_id,
                            json.dumps(history_entry, indent=2, default=str))
            return (
                "Error: generation succeeded but no output files were returned. "
                f"(prompt_id={prompt_id}"
                + (f", raw history dumped to {debug_path}" if debug_path else "")
                + ")"
            )

        # Download all output images and run safety filter on each
        safe_files: list[str] = []  # images cleared for the LLM
        soft_files: list[str] = []  # images with soft-only detections (passed + notice)
        soft_detections: list[str] = []  # all soft label names across all soft images
        hard_count = 0  # number of images withheld due to hard block

        for img in image_refs:
            filename = img.get("filename")
            subfolder = img.get("subfolder", "")
            img_type = img.get("type", "output")
            if not filename:
                continue
            try:
                path = _download(filename, subfolder, img_type)
            except Exception as e:
                logger.warning("comfyui: failed to download %s: %s", filename, e)
                continue

            if not _filter_enabled:
                safe_files.append(str(path))
                continue

            try:
                result = apply_filter(
                    path,
                    _module_dir,
                    _hard_blocked_ids,
                    _soft_blocked_ids,
                    _filter_score,
                )
            except Exception as fe:
                logger.warning("comfyui: safety filter error on %s: %s", path.name, fe)
                safe_files.append(str(path))  # pass through on filter error
                continue

            if result.hard_triggered:
                # Hard block: delete the file and do NOT pass it to the LLM
                hard_count += 1
                logger.info(
                    "comfyui: HARD BLOCK — withholding %s (triggered: %s)",
                    path.name, result.hard_blocked,
                )
                try:
                    path.unlink()
                except Exception:
                    pass
            elif result.soft_triggered:
                # Soft block: censored in place, pass to LLM with notice
                soft_files.append(str(path))
                soft_detections.extend(result.soft_blocked)
                logger.info(
                    "comfyui: soft block — passing %s with notice (triggered: %s)",
                    path.name, result.soft_blocked,
                )
            else:
                safe_files.append(str(path))

        if _unload_after:
            _free_memory()
        lines = []
        all_passed_files = safe_files + soft_files
        if all_passed_files:
            names = ", ".join(Path(p).name for p in all_passed_files)
            lines.append(f"Generated: {names}")
            lines.extend(all_passed_files)

        if hard_count > 0:
            lines.append(
                f"[Safety filter: {hard_count} image(s) were withheld and not shown "
                f"because they contained hard-blocked content.]"
            )

        if soft_detections:
            unique = sorted(set(soft_detections))
            lines.append(
                f"[Safety filter notice: the following content was automatically censored "
                f"(blacked out) in the image(s) above: {', '.join(unique)}]"
            )

        if not all_passed_files:
            lines.append("No images were cleared to display.")

        lines.extend(warnings)

        return "\n".join(lines)

    agent.tool_handler.register_tool(
        generate_image_comfyui, always_on=False, required_permissions={Permission.IMAGE_GEN}
    )
