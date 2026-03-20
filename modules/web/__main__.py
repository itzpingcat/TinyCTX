"""
modules/web/__main__.py

Registers web tools into the agent loop's tool_handler:
  - web_search     — DuckDuckGo text search
  - http_request   — generic async HTTP (GET/POST/etc.)
  - navigate       — open a URL in Playwright, returns element map
  - click          — click an element
  - type_text      — type into a field
  - extract_text   — get visible text from element or whole page
  - extract_html   — get HTML from element or whole page
  - screenshot     — save screenshot to workspace/downloads/
  - wait_for       — wait for element state
  - manage_browser — adjust settings or close the browser

One Playwright browser instance lives on the AgentLoop for the session lifetime.
It is created lazily on first use and closed on reset() via a registered hook.

Convention: register(agent) — no imports from contracts or gateway.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

import aiohttp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENGINE_PREFIXES = ("role=", "text=", "css=", "xpath=", "id=", "data-testid=")
_KNOWN_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "menuitem", "option", "heading", "img", "listitem", "list",
    "menu", "tab", "tabpanel", "tablist", "slider", "switch",
    "progressbar", "alert", "dialog",
}


def _looks_like_css(s: str) -> bool:
    return any(ch in s for ch in "#.[]>+~:*") or (
        s.islower() and s.replace("-", "").isalnum()
    )


def _strip_quotes(s: str) -> Optional[str]:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return None


# ---------------------------------------------------------------------------
# Per-session browser state (stored on agent instance)
# ---------------------------------------------------------------------------

_STATE_KEY = "_web_module"


def _state(agent) -> dict:
    if not hasattr(agent, _STATE_KEY):
        setattr(agent, _STATE_KEY, {
            "playwright": None,
            "browser":    None,
            "page":       None,
            "settings": {
                "headless":               False,
                "timeout_ms":             30000,
                "wait_until":             "domcontentloaded",
                "shift_enter_for_newline": True,
                "ignore_tags":            ["script", "style"],
                "max_discovery_elements": 40,
            },
            "downloads_dir": None,
        })
    return getattr(agent, _STATE_KEY)


async def _ensure_page(agent):
    """Lazily create a Playwright browser page for this session."""
    from playwright.async_api import async_playwright

    st = _state(agent)
    if st["page"] is not None:
        return st["page"]

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=st["settings"]["headless"])
    page = await browser.new_page()
    st["playwright"] = pw
    st["browser"]    = browser
    st["page"]       = page
    return page


async def _close_browser(agent) -> str:
    st = _state(agent)
    try:
        if st["browser"]:
            await st["browser"].close()
    finally:
        if st["playwright"]:
            await st["playwright"].stop()
    st["playwright"] = None
    st["browser"]    = None
    st["page"]       = None
    return "Browser closed."


async def _locate(agent, target: str, nth: int = 0, exact: Optional[bool] = None):
    page = await _ensure_page(agent)
    t = target.strip()

    if t.startswith(_ENGINE_PREFIXES):
        return page.locator(t).nth(nth)

    quoted = _strip_quotes(t)
    if quoted is not None:
        return page.get_by_text(quoted, exact=True if exact is None else exact).nth(nth)

    if _looks_like_css(t):
        loc = page.locator(t)
        try:
            if await loc.count() > 0:
                return loc.nth(nth)
        except Exception:
            pass

    try:
        loc = page.get_by_text(t, exact=False if exact is None else exact)
        if await loc.count() > 0:
            return loc.nth(nth)
    except Exception:
        pass

    if t in _KNOWN_ROLES:
        return page.get_by_role(t).nth(nth)

    return page.locator(t).nth(nth)


async def _dynamic_discovery(agent) -> list[dict]:
    """Walk the DOM and return a compact element map (max 40 entries)."""
    page = await _ensure_page(agent)
    st   = _state(agent)
    settings     = st["settings"]
    ignore_tags  = settings.get("ignore_tags", [])
    max_elements = settings.get("max_discovery_elements", 40)

    candidates   = page.locator("*")
    count        = await candidates.count()
    seen_content: set[str] = set()
    result: list[dict]     = []

    for i in range(count):
        if len(result) >= max_elements:
            break
        try:
            handle = await candidates.nth(i).element_handle()
            if not handle:
                continue

            tag_name = await handle.evaluate("el => el.tagName.toLowerCase()")
            if tag_name in ignore_tags:
                continue

            has_text_children = await handle.evaluate("""
                el => Array.from(el.children).some(
                    child => child.innerText && child.innerText.trim().length > 0
                )
            """)
            if has_text_children:
                continue

            role = await handle.get_attribute("role") or tag_name
            raw  = await handle.inner_text()
            text = " ".join(raw.strip().split())

            bloat = text.count("\n") + text.count("\t") + len(re.findall(r" {2,}", text))
            bloat_pct = bloat / len(text) if text else 0

            if len(text) < 3 or text in seen_content or bloat_pct > 0.3:
                continue

            seen_content.add(text)
            selector = await handle.evaluate("""
                el => el.tagName.toLowerCase()
                    + (el.id ? '#' + el.id : '')
                    + (el.className
                        ? '.' + el.className.split(' ').filter(Boolean).slice(0,2).join('.')
                        : '')
            """)
            result.append({"role": role, "text": text, "selector": selector, "nth": i})
        except Exception:
            continue

    return result


# ---------------------------------------------------------------------------
# register() — wires everything into agent
# ---------------------------------------------------------------------------

def register(agent) -> None:
    try:
        from modules.web import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}

    workspace     = Path(agent.config.workspace.path).expanduser().resolve()
    downloads_dir = workspace / cfg.get("downloads_dir", "downloads")
    downloads_dir.mkdir(parents=True, exist_ok=True)

    st = _state(agent)
    st["downloads_dir"] = downloads_dir
    st["settings"].update({
        "headless":               cfg.get("headless", False),
        "timeout_ms":             cfg.get("timeout_ms", 30000),
        "wait_until":             cfg.get("wait_until", "domcontentloaded"),
        "shift_enter_for_newline": cfg.get("shift_enter_for_newline", True),
        "ignore_tags":            list(cfg.get("ignore_tags", ["script", "style"])),
        "max_discovery_elements": cfg.get("max_discovery_elements", 40),
    })

    original_reset = agent.reset

    def patched_reset():
        original_reset()
        # Use get_running_loop() — get_event_loop() is deprecated in 3.10+
        # and may raise if called outside an async context.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_close_browser(agent))
        except RuntimeError:
            pass  # no running loop at reset time — browser will be GC'd

    agent.reset = patched_reset

    # ------------------------------------------------------------------
    # Tool definitions
    # ------------------------------------------------------------------

    async def web_search(query: str, num_results: int = 5) -> str:
        """
        Search the web using DuckDuckGo and return the top results.
        Use this when the user asks about current information or if no URL is provided.

        Args:
            query: The search query string.
            num_results: How many results to return (default 5).
        """
        from duckduckgo_search import DDGS
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=num_results))
            if not results:
                return "No results found."
            lines = [f"Search results for '{query}':"]
            for i, r in enumerate(results, 1):
                lines.append(
                    f"{i}. {r.get('title','')}\n   {r.get('href','')}\n   {r.get('body','')}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"[error: {e}]"

    async def http_request(
        method: str,
        url: str,
        params: dict = None,
        data: dict = None,
        json_data: dict = None,
        headers: dict = None,
    ) -> str:
        """
        Perform a generic HTTP request (GET, POST, PUT, DELETE, PATCH, HEAD).

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH, HEAD).
            url: The target URL.
            params: Query string parameters.
            data: Form data payload.
            json_data: JSON payload.
            headers: Extra request headers.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method.upper(), url,
                    params=params,
                    data=data,
                    json=json_data,
                    headers=headers or {},
                ) as resp:
                    body_text = await resp.text()
                    try:
                        body = json.loads(body_text)
                    except Exception:
                        body = body_text
                    return json.dumps({
                        "status_code": resp.status,
                        "headers":     dict(resp.headers),
                        "body":        body,
                    }, indent=2)
        except Exception as e:
            return f"[error: {e}]"

    async def navigate(url: str) -> str:
        """
        Open a URL in the browser and return a map of interactive elements.
        Always call this before using click, type_text, or extract on a new page.

        Args:
            url: The full URL to navigate to (include https://).
        """
        st   = _state(agent)
        page = await _ensure_page(agent)
        try:
            await page.goto(
                url,
                wait_until=st["settings"]["wait_until"],
                timeout=st["settings"]["timeout_ms"],
            )
            elements = await _dynamic_discovery(agent)
            return (
                f"Navigated to {url}.\n"
                f"Elements: {json.dumps(elements, indent=2)}\n"
                "Use extract_text or extract_html to get full page content."
            )
        except Exception as e:
            return f"[error: {e}]"

    async def click(target: str, nth: int = 0, exact: bool = None) -> str:
        """
        Click an element on the current page.

        Args:
            target: CSS selector, role=..., text=..., or plain text label.
            nth: Which matching element to click (0 = first).
            exact: Whether text matching must be exact.
        """
        st = _state(agent)
        try:
            loc = await _locate(agent, target, nth=nth, exact=exact)
            await loc.wait_for(state="visible", timeout=st["settings"]["timeout_ms"])
            await loc.click(timeout=st["settings"]["timeout_ms"])
            return f"Clicked: {target!r} (nth={nth})"
        except Exception as e:
            return f"[error: {e}]"

    async def type_text(
        target: str,
        text: str,
        nth: int = 0,
        exact: bool = None,
        clear: bool = True,
    ) -> str:
        """
        Type text into a field. Append \\n to submit/press Enter.

        Args:
            target: The input field (CSS selector, role=..., or label text).
            text: Text to type. End with \\n to press Enter after.
            nth: Which matching element to target (0 = first).
            exact: Whether text matching must be exact.
            clear: Clear the field before typing (default True).
        """
        st   = _state(agent)
        page = await _ensure_page(agent)
        try:
            loc = await _locate(agent, target, nth=nth, exact=exact)
            await loc.wait_for(state="visible", timeout=st["settings"]["timeout_ms"])
            if clear:
                await loc.fill("", timeout=st["settings"]["timeout_ms"])
            await loc.click(timeout=st["settings"]["timeout_ms"])

            if "\n" in text:
                parts = text.split("\n")
                for i, part in enumerate(parts):
                    await page.keyboard.type(part, delay=0)
                    if i < len(parts) - 1:
                        if st["settings"]["shift_enter_for_newline"]:
                            await page.keyboard.press("Shift+Enter")
                        else:
                            await page.keyboard.press("Enter")
                if text.endswith("\n"):
                    await page.keyboard.press("Enter")
            else:
                await page.keyboard.type(text, delay=0)

            return f"Typed into: {target!r} (nth={nth})"
        except Exception as e:
            return f"[error: {e}]"

    async def extract_text(target: str = "", nth: int = 0, exact: bool = None) -> str:
        """
        Get the visible text content from an element or the whole page.

        Args:
            target: Element selector or label. Leave empty for the full page.
            nth: Which matching element to read (0 = first).
            exact: Whether text matching must be exact.
        """
        st   = _state(agent)
        page = await _ensure_page(agent)
        try:
            if not target:
                return await page.locator("html").inner_text(
                    timeout=st["settings"]["timeout_ms"]
                )
            loc = await _locate(agent, target, nth=nth, exact=exact)
            await loc.wait_for(state="attached", timeout=st["settings"]["timeout_ms"])
            return await loc.inner_text(timeout=st["settings"]["timeout_ms"])
        except Exception as e:
            return f"[error: {e}]"

    async def extract_html(target: str = "", nth: int = 0, exact: bool = None) -> str:
        """
        Get the HTML markup from an element or the whole page.

        Args:
            target: Element selector or label. Leave empty for the full page.
            nth: Which matching element to read (0 = first).
            exact: Whether text matching must be exact.
        """
        st   = _state(agent)
        page = await _ensure_page(agent)
        try:
            if not target:
                return await page.content()
            loc = await _locate(agent, target, nth=nth, exact=exact)
            await loc.wait_for(state="attached", timeout=st["settings"]["timeout_ms"])
            return await loc.inner_html(timeout=st["settings"]["timeout_ms"])
        except Exception as e:
            return f"[error: {e}]"

    async def screenshot(target: str = None, filename: str = None, nth: int = 0, exact: bool = None) -> str:
        """
        Take a screenshot of the page or a specific element.
        Saved to workspace/downloads/<filename>.

        Args:
            target: Element to screenshot. Leave empty for the full page.
            filename: Output filename (default: screenshot_<timestamp>.png).
            nth: Which matching element to capture (0 = first).
            exact: Whether text matching must be exact.
        """
        st   = _state(agent)
        page = await _ensure_page(agent)

        if not filename:
            filename = f"screenshot_{int(time.time())}.png"
        path = st["downloads_dir"] / filename

        try:
            if target:
                loc = await _locate(agent, target, nth=nth, exact=exact)
                await loc.screenshot(path=str(path))
                return f"Element screenshot saved to {path}"
            else:
                await page.screenshot(path=str(path), full_page=True)
                return f"Page screenshot saved to {path}"
        except Exception as e:
            return f"[error: {e}]"

    async def wait_for(
        target: str,
        state: str = "visible",
        nth: int = 0,
        exact: bool = None,
    ) -> str:
        """
        Wait for an element to reach a given state before continuing.

        Args:
            target: The element to wait for.
            state: Target state: attached, detached, visible, or hidden.
            nth: Which matching element to watch (0 = first).
            exact: Whether text matching must be exact.
        """
        st = _state(agent)
        try:
            loc = await _locate(agent, target, nth=nth, exact=exact)
            await loc.wait_for(state=state, timeout=st["settings"]["timeout_ms"])
            return f"Element {target!r} reached state '{state}' (nth={nth})"
        except Exception as e:
            return f"[error: {e}]"

    async def manage_browser(action: str, key: str = None, value: str = None) -> str:
        """
        Manage the Playwright browser session and settings.

        Args:
            action: One of: close, view_settings, set_setting, add_ignore_tag, remove_ignore_tag, list.
            key: Setting key (required for set_setting).
            value: New value (required for set_setting, add_ignore_tag, remove_ignore_tag).
        """
        st = _state(agent)
        a  = action.lower().strip()
        valid = ["close", "view_settings", "set_setting", "add_ignore_tag", "remove_ignore_tag", "list"]

        if a == "list":
            return f"Valid actions: {valid}"

        elif a == "close":
            return await _close_browser(agent)

        elif a == "view_settings":
            return json.dumps(st["settings"], indent=2)

        elif a == "set_setting":
            if not key or value is None:
                return "Error: set_setting requires both key and value."
            if key not in st["settings"]:
                return f"Error: unknown setting '{key}'. Valid: {list(st['settings'].keys())}"
            current = st["settings"][key]
            if isinstance(current, bool):
                st["settings"][key] = value.lower() in ("true", "1", "yes")
            elif isinstance(current, int):
                st["settings"][key] = int(value)
            else:
                st["settings"][key] = value
            return f"Setting '{key}' updated to {st['settings'][key]!r}."

        elif a == "add_ignore_tag":
            if not value:
                return "Error: add_ignore_tag requires value."
            if value not in st["settings"]["ignore_tags"]:
                st["settings"]["ignore_tags"].append(value)
                return f"Tag '{value}' added to ignore list."
            return f"Tag '{value}' already ignored."

        elif a == "remove_ignore_tag":
            if not value:
                return "Error: remove_ignore_tag requires value."
            if value in st["settings"]["ignore_tags"]:
                st["settings"]["ignore_tags"].remove(value)
                return f"Tag '{value}' removed from ignore list."
            return f"Tag '{value}' not in ignore list."

        else:
            return f"Error: unknown action '{action}'. Valid: {valid}"

    for fn in (
        web_search,
        http_request,
        navigate,
        click,
        type_text,
        extract_text,
        extract_html,
        screenshot,
        wait_for,
        manage_browser,
    ):
        agent.tool_handler.register_tool(fn)