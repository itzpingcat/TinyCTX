"""
modules/web/__main__.py

Registers web tools into the agent loop's tool_handler:
  - web_search     — DuckDuckGo text search
  - open_url       — fetch or browser-render a URL; returns elements, text, or HTML
  - http_request   — generic async HTTP (GET/POST/etc.)
  - click          — click an element on the current browser page
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
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp


# ---------------------------------------------------------------------------
# SSRF guard — block requests to private/loopback IPs and non-http(s) schemes
# ---------------------------------------------------------------------------
import ipaddress
import socket

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("100.64.0.0/10"),    # carrier-grade NAT
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # ULA
]


def _is_private_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False


def _check_ssrf(url: str) -> Optional[str]:
    """Return an error string if the URL should be blocked for SSRF reasons."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "[error: invalid URL]"
    if parsed.scheme.lower() not in ("http", "https"):
        return f"[error: scheme '{parsed.scheme}' is not allowed; use http or https]"
    host = parsed.hostname or ""
    if not host:
        return "[error: URL has no host]"
    # Block bare IP literals that are private
    if _is_private_ip(host):
        return f"[error: requests to private/loopback addresses are not allowed ({host})]"
    # Resolve hostname and check each resulting IP
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            addr = info[4][0]
            if _is_private_ip(addr):
                return f"[error: hostname '{host}' resolves to a private address ({addr}) — request blocked]"
    except socket.gaierror:
        pass  # unresolvable host — let aiohttp handle it
    return None


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
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
_HEADING_PREFIX = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}
_IGNORED_TEXT_TAGS = {
    "canvas", "head", "meta", "link", "noscript", "script", "style", "svg", "title",
}
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _looks_like_css(s: str) -> bool:
    return any(ch in s for ch in "#.[]>+~:*") or (
        s.islower() and s.replace("-", "").isalnum()
    )


def _strip_quotes(s: str) -> Optional[str]:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return None


def _normalise_inline_ws(text: str) -> str:
    return " ".join(text.split())


def _normalise_extracted_text(text: str) -> str:
    lines: list[str] = []
    last_blank = True

    for raw_line in text.replace("\r", "\n").split("\n"):
        line = _normalise_inline_ws(raw_line)
        if not line:
            if lines and not last_blank:
                lines.append("")
            last_blank = True
            continue
        lines.append(line)
        last_blank = False

    while lines and not lines[-1]:
        lines.pop()

    return "\n".join(lines).strip()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self, ignored_tags: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_tags = ignored_tags
        self._ignored_depth = 0
        self._pre_depth = 0
        self._chunks: list[str] = []
        self._list_stack: list[tuple[str, int]] = []  # (tag, counter)
        self._current_href: str = ""
        self._link_text_chunks: list[str] = []
        self._in_link = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if tag in self._ignored_tags:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return

        if tag == "pre":
            self._pre_depth += 1
            self._chunks.append("\n")
            return

        if tag == "br":
            self._chunks.append("\n")
            return

        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

        if tag in _HEADING_PREFIX:
            self._chunks.append(_HEADING_PREFIX[tag])
            return

        if tag == "li":
            depth   = sum(1 for t, _ in self._list_stack if t in ("ul", "ol"))
            indent  = "  " * max(0, depth - 1)
            if self._list_stack and self._list_stack[-1][0] == "ol":
                t, n = self._list_stack[-1]
                n += 1
                self._list_stack[-1] = (t, n)
                self._chunks.append(f"{indent}{n}. ")
            else:
                self._chunks.append(f"{indent}- ")
            return

        if tag in ("ul", "ol"):
            self._list_stack.append((tag, 0))
            return

        if tag == "a":
            attrs_map = {k: v for k, v in attrs}
            href = attrs_map.get("href", "") or ""
            if href and not href.startswith(("javascript:", "#", "mailto:")):
                self._current_href = href
                self._link_text_chunks = []
                self._in_link = True
            return

        if tag == "img":
            attrs_map = {k: v for k, v in attrs}
            alt = (attrs_map.get("alt") or "").strip()
            if alt:
                self._chunks.append(f"[img: {alt}]")
            return

        if tag == "hr":
            self._chunks.append("\n---\n")
            return

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ignored_tags:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return

        if tag == "pre":
            if self._pre_depth:
                self._pre_depth -= 1
            self._chunks.append("\n")
            return

        if tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._chunks.append("\n")
            return

        if tag == "a" and self._in_link:
            link_text = "".join(self._link_text_chunks).strip()
            if link_text:
                self._chunks.append(f"{link_text} ({self._current_href})")
            self._in_link = False
            self._current_href = ""
            self._link_text_chunks = []
            return

        if tag in _HEADING_PREFIX or tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not data:
            return
        if self._pre_depth:
            self._chunks.append(data)
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_link:
            self._link_text_chunks.append(text)
        else:
            self._chunks.append(text + " ")

    def get_text(self) -> str:
        return _normalise_extracted_text("".join(self._chunks))


def _html_to_text(html_text: str, extra_ignored_tags: list[str] | None = None) -> str:
    ignored = _IGNORED_TEXT_TAGS | {tag.lower() for tag in (extra_ignored_tags or [])}
    parser = _HTMLTextExtractor(ignored)
    parser.feed(html_text)
    parser.close()
    return parser.get_text()

def _extract_html_title(html_text: str) -> Optional[str]:
    match = _TITLE_RE.search(html_text)
    if not match:
        return None
    title = re.sub(r"<[^>]+>", " ", match.group(1))
    title = _normalise_inline_ws(title)
    return title or None


def _truncate_content(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


def _decode_search_result_href(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://duckduckgo.com" + href

    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [None])[0]
        if uddg:
            return unquote(uddg)
    return href


class _DuckDuckGoResultParser(HTMLParser):
    def __init__(self, max_results: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_results = max_results
        self.results: list[dict[str, str]] = []
        self._capture_title = False
        self._capture_snippet = False
        self._title_chunks: list[str] = []
        self._snippet_chunks: list[str] = []
        self._current_href = ""

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        attrs_map = {k: v for k, v in attrs}
        classes = set((attrs_map.get("class") or "").split())

        if (
            tag == "a"
            and "result__a" in classes
            and len(self.results) < self._max_results
        ):
            self._capture_title = True
            self._title_chunks = []
            self._current_href = attrs_map.get("href", "")
            return

        if self.results and "result__snippet" in classes:
            self._capture_snippet = True
            self._snippet_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture_title and tag == "a":
            title = _normalise_inline_ws("".join(self._title_chunks))
            href = _decode_search_result_href(self._current_href)
            if title and href:
                self.results.append({"title": title, "href": href, "body": ""})
            self._capture_title = False
            self._title_chunks = []
            self._current_href = ""
            return

        if self._capture_snippet and tag in {"a", "div", "span"}:
            snippet = _normalise_inline_ws("".join(self._snippet_chunks))
            if snippet and self.results and not self.results[-1].get("body"):
                self.results[-1]["body"] = snippet
            self._capture_snippet = False
            self._snippet_chunks = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_chunks.append(data)
        elif self._capture_snippet:
            self._snippet_chunks.append(data)


def _parse_duckduckgo_results(html_text: str, max_results: int) -> list[dict[str, str]]:
    parser = _DuckDuckGoResultParser(max_results=max_results)
    parser.feed(html_text)
    parser.close()
    for result in parser.results:
        result["title"] = unescape(result.get("title", ""))
        result["body"] = unescape(result.get("body", ""))
    return parser.results


def _validate_browse_url(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except ValueError:
        return "Error: invalid URL."
    if parsed.scheme.lower() not in ("http", "https"):
        return "Error: browse_url only supports http:// or https:// URLs."
    if not parsed.netloc:
        return "Error: browse_url requires a full URL with a hostname."
    if parsed.username or parsed.password:
        return "Error: URLs with embedded credentials are not supported."
    return None

async def _search_with_duckduckgo_html(
    query: str,
    *,
    num_results: int,
    user_agent: str,
) -> list[dict[str, str]]:
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        "User-Agent": user_agent,
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            allow_redirects=True,
        ) as resp:
            body = await resp.read()
            if len(body) > 1_000_000:
                body = body[:1_000_000]
            charset = resp.charset or "utf-8"
            html_text = body.decode(charset, errors="replace")

    return _parse_duckduckgo_results(html_text, max_results=num_results)

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
    """Walk the DOM in a single JS evaluate call and return a compact element map."""
    page = await _ensure_page(agent)
    st   = _state(agent)
    settings     = st["settings"]
    ignore_tags  = list(settings.get("ignore_tags", []))
    max_elements = settings.get("max_discovery_elements", 40)

    return await page.evaluate("""
        ([ignoreTags, maxElements]) => {
            const ignore = new Set(ignoreTags);
            const seen   = new Set();
            const result = [];

            for (const el of document.querySelectorAll('*')) {
                if (result.length >= maxElements) break;
                const tag = el.tagName.toLowerCase();
                if (ignore.has(tag)) continue;

                // skip nodes whose direct children already carry the text
                const hasTextChild = Array.from(el.children).some(
                    c => c.innerText && c.innerText.trim().length > 0
                );
                if (hasTextChild) continue;

                const raw  = (el.innerText || '').trim();
                const text = raw.replace(/\\s+/g, ' ');
                if (text.length < 3 || seen.has(text)) continue;

                const bloat = (text.match(/[\\n\\t]|  +/g) || []).length;
                if (text.length > 0 && bloat / text.length > 0.3) continue;

                seen.add(text);
                const role = el.getAttribute('role') || tag;
                const cls  = Array.from(el.classList).slice(0, 2).join('.');
                const selector = tag
                    + (el.id ? '#' + el.id : '')
                    + (cls ? '.' + cls : '');
                result.push({ role, text, selector });
            }
            return result;
        }
    """, [ignore_tags, max_elements])


def _web_prompt(_ctx) -> str:
    return (
        "<web_tools>\n"
        "- Use web_search when you need discovery, current information, or you do not yet have a URL.\n"
        "- Use open_url when you have a specific URL. Returns text by default; use type='elements' when you need to click/type/interact, or type='html' for raw markup.\n"
        "- If open_url returns a captcha or login wall, retry with headless=False so the browser window becomes visible.\n"
        "- Use click, type_text, wait_for, extract_text, extract_html, or screenshot after open_url(type='elements') to interact with the loaded page.\n"
        "- Do not use shell with curl, wget, Invoke-WebRequest, or similar commands for normal web fetching.\n"
        "</web_tools>"
    )


# ---------------------------------------------------------------------------
# register() — wires everything into agent
# ---------------------------------------------------------------------------

def register(agent) -> None:
    try:
        from TinyCTX.modules.web import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}
    runtime_web_cfg: dict = {}
    if hasattr(agent.config, "extra") and isinstance(agent.config.extra, dict):
        runtime_web_cfg = agent.config.extra.get("web", {})
    cfg = {**cfg, **{k: v for k, v in runtime_web_cfg.items() if k != "tools"}}

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
        "browse_max_bytes":       int(cfg.get("browse_max_bytes", 2000000)),
        "browse_max_chars":       int(cfg.get("browse_max_chars", 20000)),
        "browse_user_agent":      str(cfg.get("browse_user_agent", "TinyCTX/1.1")),
    })

    original_reset = agent.reset

    def patched_reset(*args, **kwargs):
        original_reset(*args, **kwargs)
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
        num_results = int(num_results)
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                DDGS = None

            results: list[dict] = []
            if DDGS is not None:
                try:
                    with DDGS() as ddgs:
                        results = list(ddgs.text(query, max_results=num_results))
                except Exception:
                    results = []

            if not results:
                results = await _search_with_duckduckgo_html(
                    query,
                    num_results=num_results,
                    user_agent=st["settings"]["browse_user_agent"],
                )

            if not results:
                return "No results found."

            lines = [f"Search results for '{query}':"]
            for i, r in enumerate(results, 1):
                lines.append(
                    f"{i}. {r.get('title','')}\n   {r.get('href','')}\n   {r.get('body','')}"
                )
            lines.append(
                "If you need the contents of a specific result URL, prefer browse_url() "
                "or navigate() instead of shell-based curl/Invoke-WebRequest."
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
        ssrf_err = _check_ssrf(url)
        if ssrf_err:
            return ssrf_err
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

    async def open_url(
        url: str,
        type: str = "text",
        headless: bool = None,
    ) -> str:
        """
        Open a URL in the browser and return its content.
        If you hit a captcha or login wall, call with headless=False so the
        browser window becomes visible and you can solve it manually.

        Args:
            url: The full URL to open (include https://).
            type: What to return — "text" (visible page text, default),
                  "html" (raw HTML markup), or "elements" (interactive element map).
            headless: Override the session headless setting for this request.
                      None = use session default. False = show browser window
                      (useful for captchas). True = force headless.
        """
        st  = _state(agent)
        err = _validate_browse_url(url)
        if err:
            return err
        # Also guard against SSRF to private/internal addresses.
        ssrf_err = _check_ssrf(url)
        if ssrf_err:
            return ssrf_err

        mode = type.lower().strip()
        if mode not in ("elements", "text", "html"):
            return "Error: type must be 'elements', 'text', or 'html'."

        try:
            # Override headless for this request if specified
            original_headless = st["settings"]["headless"]
            if headless is not None:
                st["settings"]["headless"] = headless
                # If a browser is already open with the wrong headless mode, close and reopen
                if st["browser"] is not None:
                    await _close_browser(agent)

            try:
                page     = await _ensure_page(agent)
                response = await page.goto(
                    url,
                    wait_until=st["settings"]["wait_until"],
                    timeout=st["settings"]["timeout_ms"],
                )
                status = response.status if response else 200

                if mode == "elements":
                    elements = await _dynamic_discovery(agent)
                    return (
                        f"Opened {url} (status {status}).\n"
                        f"Elements: {json.dumps(elements, indent=2)}\n"
                        "Use open_url with type='text' or type='html' for full page content."
                    )

                html = await page.content()
                if mode == "html":
                    content = html
                else:
                    content = _html_to_text(html, st["settings"]["ignore_tags"])
                content, truncated = _truncate_content(content, st["settings"]["browse_max_chars"])
                title = await page.title() or _extract_html_title(html) or ""

            finally:
                # Always restore the session headless setting
                if headless is not None:
                    st["settings"]["headless"] = original_headless

            suffix     = "\n[truncated]" if truncated else ""
            title_line = f"# {title}\n" if title else ""
            return f"{title_line}{url} (status {status})\n\n{content}{suffix}"

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

    async def screenshot_browser(target: str = None, filename: str = None, nth: int = 0, exact: bool = None) -> str:
        """
        Take a screenshot of a browser page or a specific element.
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
        # Strip any directory components — prevent path traversal outside downloads_dir.
        safe_name = Path(filename).name
        if not safe_name:
            safe_name = f"screenshot_{int(time.time())}.png"
        path = (st["downloads_dir"] / safe_name).resolve()
        if not str(path).startswith(str(st["downloads_dir"].resolve())):
            return "[error: filename escapes downloads directory]"

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

    # Defaults: web_search, and navigate are always_on; the rest are deferred.
    # Can be overridden per-tool via config: web.tools.<tool_name>: always_on|deferred|disabled
    try:
        from TinyCTX.modules.web import EXTENSION_META as _META
        _tools_cfg: dict = _META.get("default_config", {}).get("tools", {})
    except ImportError:
        _tools_cfg = {}
    # Also allow runtime config.yaml override under web.tools:
    _runtime_tools_cfg: dict = {}
    if runtime_web_cfg:
        _runtime_tools_cfg = runtime_web_cfg.get("tools", {})
    _tools_cfg = {**_tools_cfg, **_runtime_tools_cfg}

    _WEB_DEFAULTS: dict[str, bool] = {
        "web_search":     True,
        "open_url":       True,
        "http_request":   False,
        "click":          False,
        "type_text":      False,
        "extract_text":   False,
        "extract_html":   False,
        "screenshot_browser":     False,
        "wait_for":       False,
        "manage_browser": False,
    }

    for fn in (
        web_search,
        open_url,
        http_request,
        click,
        type_text,
        extract_text,
        extract_html,
        screenshot_browser,
        wait_for,
        manage_browser,
    ):
        vis = str(_tools_cfg.get(fn.__name__, "")).lower().strip()
        if vis == "disabled":
            continue
        always_on = _WEB_DEFAULTS[fn.__name__] if vis == "" else vis == "always_on"
        agent.tool_handler.register_tool(fn, always_on=always_on)
