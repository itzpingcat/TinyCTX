"""
modules/comfyui

generate_image_comfyui — runs an admin-provided ComfyUI workflow in-process
(no subprocess).

Workflows are JSON files dropped by the admin into the instance's read-only
extra-config directory, resolved via utils/instance.py::runtime_config_dir():

    <instance>/config/comfyui/<name>.json

The agent picks a workflow by name (bare filename, no extension). Available
names are discovered once at @hook(HookType.STARTUP) and baked into the
tool's docstring.

Uses the raw ComfyUI **API v1** surface (POST /prompt, GET /history/{id},
GET /view). This talks directly to stock ComfyUI (default 127.0.0.1:8188) —
no comfy-api-proxy or Comfy Cloud v2 job surface required.

Config (read from the top-level `comfyui:` key in config.yaml, via
config.extra — same mechanism as the `mcp:` block):

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

Everything here is process-lifetime — workflow discovery, safety-filter
config, and the ComfyUI HTTP client all come from config alone, nothing
per-cycle — so, unlike modules/present or modules/sysops, this is a plain
@tool with all setup done once in @hook(HookType.STARTUP). If no workflows
are configured, the tool stays registered (decorators tag unconditionally)
but returns a clear error on call instead of the pre-Module version's
"don't register the tool at all" — a caller with IMAGE_GEN but no configured
workflows now gets an informative error rather than "tool not found".
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path

import requests

from TinyCTX.decorators import hook, tool
from TinyCTX.hooks import HookType
from TinyCTX.module import Module
from TinyCTX.permissions import Permission
from TinyCTX.utils.instance import runtime_config_dir

from .filter import apply_filter, resolve_blocked_ids

logger = logging.getLogger(__name__)

# Matches a JSON string value that is *exactly* one marker, e.g.
# "MARKER>>width<<MARKER" — the whole field, not embedded in other text.
# This lets a marker stand in for a non-string field (seed/width/height
# are normally JSON ints); on an exact match we substitute the real
# typed value instead of stringifying it. A marker embedded inside a
# longer string (e.g. prompts) still substitutes as text via the
# partial-match regex below.
_EXACT_MARKER_RE   = re.compile(r'^MARKER>>([^<\n]+?)<<MARKER$')
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


_GENERATE_DOC_TEMPLATE = """Generate an image using a ComfyUI workflow.

        workflow: name of the workflow to run, one of {workflow_names}.
        dimensions: "WIDTHxHEIGHT", e.g. "1024x1024". A workflow that
            doesn't support custom dimensions ignores this (see returned
            warnings).
        seed: default 0. A workflow that doesn't support seeding ignores
            this (see returned warnings).
        """


class ComfyUI(Module):
    """generate_image_comfyui tool. Runs an admin-provided ComfyUI workflow
    from config/comfyui/."""

    settings = {
        "timeout":      {"default": 300, "type": "int", "description": "Seconds to wait for the job to finish."},
        "host":         {"default": "127.0.0.1", "type": "str", "description": "ComfyUI host."},
        "port":         {"default": 8188, "type": "int", "description": "ComfyUI port."},
        "api_key":      {"default": None, "type": "str", "description": "Optional Bearer token (reverse-proxied setups)."},
        "unload_after": {"default": True, "type": "bool", "description": "POST /free (unload models, free VRAM) when done."},
        "safety_filter": {
            "default": {"enabled": False, "min_score": 0.2, "hard_blocked_labels": [], "soft_blocked_labels": []},
            "type": "dict",
            "description": "enabled, min_score, hard_blocked_labels (withheld), soft_blocked_labels (censored, sent with a notice).",
        },
    }

    @hook(HookType.STARTUP)
    def load(self, runtime) -> None:
        self._module_dir     = Path(__file__).parent
        workspace_path        = Path(runtime.config.workspace.path)
        self._workflow_dir    = runtime_config_dir(workspace_path) / "comfyui"
        self._output_dir      = workspace_path / "outputs" / "comfyui"

        self._comfy_url    = f"http://{self.config['host']}:{self.config['port']}"
        self._timeout      = int(self.config["timeout"])
        self._unload_after = bool(self.config["unload_after"])
        self._api_key      = self.config.get("api_key")
        self._client_id    = str(uuid.uuid4())

        if self._workflow_dir.is_dir():
            self._workflow_names = sorted(p.stem for p in self._workflow_dir.glob("*.json"))
        else:
            self._workflow_names = []

        if not self._workflow_names:
            logger.warning(
                "comfyui: no workflows found in %s — generate_image_comfyui will return an error until configured",
                self._workflow_dir,
            )
        else:
            logger.info("comfyui: discovered workflows: %s", self._workflow_names)

        filter_cfg = self.config["safety_filter"]
        self._filter_enabled = bool(filter_cfg.get("enabled", False))
        self._filter_score   = float(filter_cfg.get("min_score", 0.2))
        self._hard_blocked_ids = resolve_blocked_ids(filter_cfg.get("hard_blocked_labels", []))
        self._soft_blocked_ids = resolve_blocked_ids(filter_cfg.get("soft_blocked_labels", []))

        if self._filter_enabled:
            logger.info(
                "comfyui: safety filter enabled — hard-blocking %d label(s): %s | soft-blocking %d label(s): %s",
                len(self._hard_blocked_ids), list(filter_cfg.get("hard_blocked_labels", [])),
                len(self._soft_blocked_ids), list(filter_cfg.get("soft_blocked_labels", [])),
            )
        else:
            logger.info("comfyui: safety filter disabled")

        # The tool's docstring feeds the model-visible schema description —
        # see modules/shell's STARTUP hook for why this must be set here
        # rather than as a static docstring.
        type(self).generate_image_comfyui.__doc__ = _GENERATE_DOC_TEMPLATE.format(
            workflow_names=self._workflow_names,
        )

    # ------------------------------------------------------------------
    # ComfyUI helpers (API v1: /prompt, /history, /view, /free)
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _wait_for_comfy(self, timeout: int = 5) -> None:
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                if requests.get(f"{self._comfy_url}/system_stats", headers=self._headers(), timeout=2).ok:
                    return
            except Exception as e:
                last_err = e
            time.sleep(0.5)
        raise RuntimeError(
            f"ComfyUI did not respond within {timeout}s"
            + (f": {last_err}" if last_err else "")
        )

    def _submit(self, workflow: dict) -> str:
        """Submit a workflow to the ComfyUI API v1 (POST /prompt)."""
        url = f"{self._comfy_url}/prompt"
        payload = {"prompt": workflow, "client_id": self._client_id}
        try:
            response = requests.post(url, json=payload, headers=self._headers(), timeout=5)
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

    def _poll(self, prompt_id: str, timeout: int) -> dict:
        """Poll GET /history/{id} until the job shows up as finished."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self._comfy_url}/history/{prompt_id}", headers=self._headers(), timeout=2
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

    def _download(self, filename: str, subfolder: str, img_type: str) -> Path:
        """Download an output image via GET /view."""
        response = requests.get(
            f"{self._comfy_url}/view",
            params={"filename": filename, "subfolder": subfolder, "type": img_type},
            headers=self._headers(),
            timeout=10,
            stream=True,
        )
        response.raise_for_status()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._output_dir / filename
        with open(out_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return out_path

    def _load_workflow(self, name: str) -> dict:
        """Load a workflow JSON by bare name from self._workflow_dir."""
        path = self._workflow_dir / f"{name}.json"
        try:
            with open(path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Workflow file not found: {path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in workflow file: {e}")

    def _free_memory(self) -> None:
        """POST /free to unload models and free VRAM (v1 endpoint)."""
        try:
            requests.post(
                f"{self._comfy_url}/free",
                json={"unload_models": True, "free_memory": True},
                headers=self._headers(),
                timeout=5,
            )
        except Exception as e:
            logger.debug("comfyui: /free request failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Tool
    # ------------------------------------------------------------------

    @tool(always_on=False, permissions={Permission.IMAGE_GEN})
    def generate_image_comfyui(
        self,
        workflow: str,
        positive_prompt: str,
        negative_prompt: str,
        dimensions: str = "1024x1024",
        seed: int = 0,
    ) -> str:
        if not self._workflow_names:
            return f"Error: no workflows configured — drop workflow JSON files into {self._workflow_dir}."

        # --- resolve workflow name -------------------------------------
        if not re.fullmatch(r"[A-Za-z0-9_-]+", workflow):
            return (
                f"Error: invalid workflow name '{workflow}'. "
                f"Available: {self._workflow_names}"
            )
        if workflow not in self._workflow_names:
            return f"Error: unknown workflow '{workflow}'. Available: {self._workflow_names}"

        # --- parse dimensions --------------------------------------------
        m = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", dimensions, re.IGNORECASE)
        if not m:
            return f"Error: invalid dimensions '{dimensions}' — expected 'WIDTHxHEIGHT', e.g. '1024x1024'."
        width, height = int(m.group(1)), int(m.group(2))
        if width <= 0 or height <= 0:
            return f"Error: invalid dimensions '{dimensions}' — width and height must be positive."

        try:
            raw_workflow = self._load_workflow(workflow)
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
            self._wait_for_comfy(timeout=5)
        except RuntimeError as e:
            return f"Error: {e}"

        logger.info("comfyui: submitting prompt (workflow=%s, timeout=%ds)", workflow, self._timeout)

        try:
            prompt_id = self._submit(prepared_workflow)
        except Exception as e:
            return f"Error: failed to submit prompt: {e}"

        logger.info("comfyui: job submitted: %s", prompt_id)

        try:
            history_entry = self._poll(prompt_id, timeout=self._timeout)
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
                self._output_dir.mkdir(parents=True, exist_ok=True)
                debug_path = self._output_dir / f"debug_history_{prompt_id}.json"
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
                path = self._download(filename, subfolder, img_type)
            except Exception as e:
                logger.warning("comfyui: failed to download %s: %s", filename, e)
                continue

            if not self._filter_enabled:
                safe_files.append(str(path))
                continue

            try:
                result = apply_filter(
                    path,
                    self._module_dir,
                    self._hard_blocked_ids,
                    self._soft_blocked_ids,
                    self._filter_score,
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

        if self._unload_after:
            self._free_memory()
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
