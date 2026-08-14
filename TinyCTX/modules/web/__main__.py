"""
modules/web/__main__.py

Registers web tools into the agent loop's tool_handler:
  - web_search          — DuckDuckGo text search
  - open_url            — browser-render a URL; returns elements, text, HTML, or a screenshot
  - click               — click an element on the current browser page
  - type_text           — type into a field
  - extract_text        — get visible text from element or whole page
  - extract_html        — get HTML from element or whole page
  - screenshot_browser  — save screenshot to workspace/downloads/
  - wait_for            — wait for element state
  - manage_browser      — adjust settings or close the browser

One Camoufox (anti-detect Firefox) browser instance lives on the AgentLoop for
the session lifetime. It is created lazily on first use and closed on reset()
via a registered hook. All browser tools share that single page: click,
type_text, extract_* and screenshot_browser act on whatever open_url loaded last.

Navigation settles past JS interstitials (Reddit's js_challenge, Cloudflare's
"Just a moment", etc.) before content is read — see _settle_navigation.

Convention: register_agent(agent) — no imports from contracts or gateway.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from TinyCTX.contracts import IMAGE_BLOCK_PREFIX


# ---------------------------------------------------------------------------
# SSRF guard — block requests to private/loopback IPs and non-http(s) schemes
# ---------------------------------------------------------------------------
import ipaddress
import socket

logger = logging.getLogger(__name__)

_PRIVATE_NETWORKS = [
    # IPv4
    ipaddress.ip_network("0.0.0.0/8"),         # "this" network
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),     # carrier-grade NAT (CGNAT)
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local / cloud metadata (AWS IMDSv1 etc.)
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),      # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),      # TEST-NET-1 (documentation)
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),     # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),   # TEST-NET-2 (documentation)
    ipaddress.ip_network("203.0.113.0/24"),    # TEST-NET-3 (documentation)
    ipaddress.ip_network("240.0.0.0/4"),       # reserved
    ipaddress.ip_network("255.255.255.255/32"),# broadcast
    # IPv6
    ipaddress.ip_network("::1/128"),           # loopback
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped (::ffff:192.168.x.x etc.)
    ipaddress.ip_network("64:ff9b::/96"),      # NAT64 well-known prefix
    ipaddress.ip_network("100::/64"),          # discard-only
    ipaddress.ip_network("2002::/16"),         # 6to4 (embeds IPv4)
    ipaddress.ip_network("fc00::/7"),          # ULA (fc00:: and fd00::)
    ipaddress.ip_network("fe80::/10"),         # link-local
    ipaddress.ip_network("ff00::/8"),          # multicast
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
        return "Error: invalid URL"
    if parsed.scheme.lower() not in ("http", "https"):
        return f"Error: scheme '{parsed.scheme}' is not allowed; use http or https"
    host = parsed.hostname or ""
    if not host:
        return "Error: URL has no host"
    # Block bare IP literals that are private
    if _is_private_ip(host):
        return f"Error: requests to private/loopback addresses are not allowed ({host})"
    # Resolve hostname and check each resulting IP
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            addr = info[4][0]
            if _is_private_ip(str(addr)):
                return f"Error: hostname '{host}' resolves to a private address ({addr}) — request blocked"
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
# HTML5 void elements: never receive a closing tag, so HTMLParser will never
# fire handle_endtag() for them. If one of these is also listed in
# _IGNORED_TEXT_TAGS, incrementing _ignored_depth on its start tag without a
# matching decrement permanently poisons the parse (every char of the
# document after the first stray void tag gets silently dropped). meta/link
# in <head> are the common real-world trigger.
_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
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
            # Void elements (meta, link, ...) never get a closing tag, so
            # never fire handle_endtag() - do not increment depth for them
            # or it can never be decremented back down.
            if tag not in _VOID_ELEMENTS:
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
            if tag not in _VOID_ELEMENTS and self._ignored_depth:
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
            href = _decode_search_result_href(self._current_href or "")
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

# ---------------------------------------------------------------------------
# JS interstitial handling
# ---------------------------------------------------------------------------
# Sites like Reddit and Cloudflare-fronted hosts answer the first request with a
# tiny JS challenge page that redirects (or rewrites itself) a moment later. Its
# DOM parses instantly, so wait_until="domcontentloaded" returns the *challenge*
# rather than the page, and the status is a perfectly healthy 200.

# Detection is structural, not textual. Matching on phrases like "just a moment"
# only works in English and breaks whenever a vendor reworders their page; the
# challenge *machinery* (the form, the widget iframe, the challenge-platform
# script) is what actually has to be present for a challenge to run.
_CHALLENGE_URL_MARKERS = (
    "js_challenge=",
    "__cf_chl",
    "/cdn-cgi/challenge",
    "/_incapsula_resource",
)
_CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#challenge-running",
    "#cf-chl-widget",
    "script[src*='/cdn-cgi/challenge-platform']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='challenge' i]",
    "#px-captcha",
    "#sec-cpt-if",
)


async def _looks_like_challenge(page) -> bool:
    """Is the current page an interstitial rather than real content?"""
    if any(marker in (page.url or "").lower() for marker in _CHALLENGE_URL_MARKERS):
        return True
    try:
        return bool(await page.evaluate(
            "sels => sels.some(s => document.querySelector(s) !== null)",
            list(_CHALLENGE_SELECTORS),
        ))
    except Exception:
        return False


async def _wait_for_dom_stable(page, deadline: float, *, polls: int = 3, interval: float = 0.4) -> None:
    """
    Wait until the DOM stops changing size across consecutive polls.

    This is the generic completion signal: rather than guessing when a specific
    vendor's challenge is done, wait for the document to stop being rewritten.
    It covers challenge hand-off, client-side rendering and late hydration alike.
    """
    stable  = 0
    last    = -1
    while time.monotonic() < deadline and stable < polls:
        try:
            size = await page.evaluate("document.documentElement.innerHTML.length")
        except Exception:
            return  # navigating / context torn down — caller re-checks
        stable = stable + 1 if size == last else 0
        last   = size
        await asyncio.sleep(interval)


async def _settle_navigation(page, settings, *, wait_for: Optional[str] = None) -> bool:
    """
    Wait for the page to finish becoming itself after goto().

    Order of preference, strongest signal first:
      1. `wait_for` — an explicit selector from the caller. If you know what the
         real page contains, that beats every heuristic here; a challenge cannot
         fake it, and there is no ambiguity about when it has cleared.
      2. Challenge machinery disappearing (structural, see _CHALLENGE_SELECTORS),
         re-checked after each navigation the challenge triggers.
      3. DOM size going quiet (_wait_for_dom_stable).

    Returns True if the page still looks like an interstitial when the settle
    budget runs out. Never raises — a settle failure should degrade to whatever
    content is on the page, not blow up the tool call.
    """
    timeout   = settings["timeout_ms"]
    settle_ms = settings.get("settle_timeout_ms", 15000)
    deadline  = time.monotonic() + settle_ms / 1000.0

    try:
        await page.wait_for_load_state("load", timeout=min(timeout, settle_ms))
    except Exception:
        pass

    if wait_for:
        try:
            remaining = max(0.0, deadline - time.monotonic()) * 1000
            await page.wait_for_selector(wait_for, timeout=remaining or 1)
            return False
        except Exception:
            return await _looks_like_challenge(page)

    while await _looks_like_challenge(page):
        if time.monotonic() >= deadline:
            return True
        # The challenge usually hands off by navigating; wait for that rather
        # than blind-polling, but fall back to a poll for in-place rewrites.
        try:
            async with page.expect_navigation(
                timeout=max(0.0, deadline - time.monotonic()) * 1000 or 1
            ):
                pass
        except Exception:
            await asyncio.sleep(0.5)
        try:
            await page.wait_for_load_state("load", timeout=min(timeout, settle_ms))
        except Exception:
            pass

    await _wait_for_dom_stable(page, deadline)
    return False


# ---------------------------------------------------------------------------
# Screenshot output
# ---------------------------------------------------------------------------

def _screenshot_path(st: dict, filename: Optional[str]) -> Path | str:
    """Resolve a screenshot path under output_dir, or return an error string."""
    safe_name = Path(filename).name if filename else ""
    if not safe_name:
        safe_name = f"screenshot_{int(time.time())}.png"
    if not safe_name.lower().endswith(".png"):
        safe_name += ".png"
    out_dir = st["output_dir"]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (
            f"Error: could not create the screenshot directory {out_dir}: {exc}. "
            "Check that the workspace is writable by the container user."
        )
    out_dir = out_dir.resolve()
    path = (out_dir / safe_name).resolve()
    if not str(path).startswith(str(out_dir)):
        return "Error: filename escapes the output directory"
    return path


def _image_result(path: Path, inline: bool, max_bytes: int) -> str:
    """
    Return a screenshot to the model.

    The file is always written to output_dir. inline=True additionally emits the
    IMAGE_BLOCK sentinel (see contracts.IMAGE_BLOCK_PREFIX) that
    agent._execute_tool unwraps into a real image block, with the saved path as
    the trailing caption so the model knows where the file landed.

    Oversized images are never inlined — a full-page PNG of a long page can run
    to tens of megabytes, which is a large amount of context to spend on one
    tool call. Past max_bytes the path is returned instead.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"Error: screenshot saved to {path} but could not be read back: {exc}"

    if not inline:
        return f"Screenshot saved to {path}"

    if size > max_bytes:
        logger.info("web: screenshot %s is %d bytes, over the inline limit", path, size)
        return (
            f"Screenshot saved to {path} ({size // 1024} KB) — too large to show inline "
            f"(limit {max_bytes // 1024} KB). View it with view(), or capture a single "
            "element with screenshot_browser(target=...) for a smaller image."
        )

    try:
        b64 = base64.b64encode(path.read_bytes()).decode()
    except OSError as exc:
        return f"Error: screenshot saved to {path} but could not be read back: {exc}"
    return f"{IMAGE_BLOCK_PREFIX}image/png;{b64}\n(also saved to {path})"


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
            "camoufox":   None,
            "browser":    None,
            "page":       None,
            "settings": {
                "timeout_ms":             30000,
                "wait_until":             "domcontentloaded",
                "shift_enter_for_newline": True,
                "ignore_tags":            ["script", "style"],
                "max_discovery_elements": 40,
            },
            "output_dir": None,
        })
    return getattr(agent, _STATE_KEY)


async def _ensure_page(agent):
    """Lazily create a Camoufox browser page for this session."""
    from camoufox.async_api import AsyncCamoufox

    st = _state(agent)
    if st["page"] is not None:
        return st["page"]

    # headless may be True, False, or "virtual". "virtual" runs a real headful
    # Firefox inside Xvfb: Camoufox recommends it because true-headless Firefox
    # leaks detectable signals that trip bot checks (Reddit, Cloudflare).
    headless = st.get("_headless", "virtual")
    # Firefox's own content-process sandbox is left ON — it launches fine under
    # the container's cap_drop: ALL / no-new-privileges (see compose.yaml).
    # Note there is no --no-sandbox flag to pass here even if we wanted one:
    # that is a Chromium flag, and Firefox ignores it.
    camoufox = AsyncCamoufox(headless=headless)
    browser = await camoufox.__aenter__()
    page = await browser.new_page()

    # Block requests (including redirects) to private/internal addresses.
    async def _ssrf_route_handler(route, request):
        err = _check_ssrf(request.url)
        if err:
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    await page.route("**/*", _ssrf_route_handler)

    st["camoufox"] = camoufox
    st["browser"]  = browser
    st["page"]     = page
    return page


async def _close_browser(agent) -> str:
    st = _state(agent)
    try:
        if st["camoufox"]:
            await st["camoufox"].__aexit__(None, None, None)
    finally:
        pass
    st["camoufox"] = None
    st["browser"]  = None
    st["page"]     = None
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
        return page.get_by_role(t).nth(nth)  # type: ignore[arg-type]

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

# ---------------------------------------------------------------------------
# register() — wires everything into agent
# ---------------------------------------------------------------------------

def register_agent(agent) -> None:
    try:
        from TinyCTX.modules.web import EXTENSION_META
        cfg: dict = EXTENSION_META.get("default_config", {})
    except ImportError:
        cfg = {}
    runtime_web_cfg: dict = {}
    if hasattr(agent.config, "extra") and isinstance(agent.config.extra, dict):
        runtime_web_cfg = agent.config.extra.get("web", {})
    cfg = {**cfg, **{k: v for k, v in runtime_web_cfg.items() if k != "tools"}}

    workspace  = Path(agent.config.workspace.path).expanduser().resolve()
    # NOT created here. The workspace is a bind mount owned by the host UID, so
    # creating a new top-level dir can raise PermissionError for the container
    # user — and register_agent exceptions are swallowed by module_registry,
    # which would silently drop every tool in this module. Created on demand at
    # screenshot time instead (same as custom_modules/anima).
    output_dir = workspace / cfg.get("output_dir", "outputs/browser")

    st = _state(agent)
    st["output_dir"] = output_dir
    st["settings"].update({
        "timeout_ms":             cfg.get("timeout_ms", 30000),
        "wait_until":             cfg.get("wait_until", "domcontentloaded"),
        "shift_enter_for_newline": cfg.get("shift_enter_for_newline", True),
        "ignore_tags":            list(cfg.get("ignore_tags", ["script", "style"])),
        "max_discovery_elements": cfg.get("max_discovery_elements", 40),
        "browse_max_bytes":       int(cfg.get("browse_max_bytes", 2000000)),
        "browse_max_chars":       int(cfg.get("browse_max_chars", 20000)),
        "browse_user_agent":      str(cfg.get("browse_user_agent", "TinyCTX/1.1")),
        "settle_timeout_ms":      int(cfg.get("settle_timeout_ms", 15000)),
        "screenshot_max_bytes":   int(cfg.get("screenshot_max_bytes", 1500000)),
    })

    # Launch mode: True | False | "virtual" (Xvfb-backed headful — the default,
    # and the only mode that survives most bot checks). config `headless` was
    # previously parsed but never applied; it is honoured here.
    _cfg_headless = cfg.get("headless", "virtual")
    if isinstance(_cfg_headless, str):
        _cfg_headless = _cfg_headless.strip().lower()
        if _cfg_headless in ("true", "yes", "1"):
            _cfg_headless = True
        elif _cfg_headless in ("false", "no", "0"):
            _cfg_headless = False
        elif _cfg_headless != "virtual":
            _cfg_headless = "virtual"
    st["_headless"] = _cfg_headless
    st["_default_headless"] = _cfg_headless

    original_reset = getattr(agent, 'reset', None)

    if original_reset is not None:
        def patched_reset(*args, **kwargs):
            original_reset(*args, **kwargs)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_close_browser(agent))
            except RuntimeError:
                pass
        agent.reset = patched_reset

    # ------------------------------------------------------------------
    # Tool definitions
    # ------------------------------------------------------------------

    async def web_search(query: str, num_results: int = 5) -> str:
        """
        Search the web using DuckDuckGo and return the top results.
        Use this when the user asks about current information or if no URL is provided.

        Backed by the `ddgs` library, falling back to scraping html.duckduckgo.com
        if it is unavailable. DuckDuckGo rate-limits aggressively, so an empty
        result set often means throttling rather than "nothing exists" — retry or
        rephrase before concluding a topic has no coverage.

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
                "If you need the contents of a specific result URL, prefer open_url() "
                "instead of shell-based curl/Invoke-WebRequest."
            )
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    # http_request removed — deprecated.

    async def open_url(
        url: str,
        type: str = "text",
        headless: bool | None = None,
        filename: str | None = None,
        inline: bool = True,
        wait_for: str | None = None,
    ) -> str:
        """
        Open a URL in the browser and return its content.

        Uses Camoufox (anti-detect Firefox) driving one shared page per session —
        the page stays loaded, so click/type_text/extract_text/screenshot_browser
        operate on it afterwards. Waits past JS interstitials (Reddit, Cloudflare)
        before reading content.

        Args:
            url: The full URL to open (include https://).
            type: What to return — "text" (visible page text, default),
                  "html" (raw HTML markup), "elements" (interactive element map),
                  or "screenshot" (full-page PNG saved to workspace/outputs/browser/).
            headless: Leave unset to use the configured launch mode. Set False to
                      show a real browser window — only works where a display is
                      available, not in the default container.
            filename: Output filename, for type="screenshot" only
                      (default: screenshot_<timestamp>.png).
            inline: For type="screenshot": return the image itself so you can see it
                    (default). Set False to get the saved file path as text instead.
            wait_for: CSS selector that only the real page has. Strongly preferred on
                      sites behind a bot check — waiting for known content is more
                      reliable than guessing when the interstitial finished.
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
        if mode not in ("elements", "text", "html", "screenshot"):
            return "Error: type must be 'elements', 'text', 'html', or 'screenshot'."

        want_headless = st.get("_default_headless", "virtual") if headless is None else headless

        try:
            # If a browser is already open with a mismatched headless mode, close it.
            if st["browser"] is not None and st.get("_headless") != want_headless:
                await _close_browser(agent)
            st["_headless"] = want_headless

            page     = await _ensure_page(agent)
            response = await page.goto(
                url,
                wait_until=st["settings"]["wait_until"],
                timeout=st["settings"]["timeout_ms"],
            )
            status = response.status if response else 200
            stuck  = await _settle_navigation(page, st["settings"], wait_for=wait_for)
            stuck_note = (
                "\n[warning: page still looks like a bot-check interstitial after "
                "waiting; content below may be incomplete]"
                if stuck else ""
            )

            if mode == "screenshot":
                path = _screenshot_path(st, filename)
                if isinstance(path, str):
                    return path
                await page.screenshot(path=str(path), full_page=True)
                if stuck:
                    logger.warning("web: screenshot of %s taken on a likely interstitial", page.url)
                return _image_result(path, inline, st["settings"]["screenshot_max_bytes"])

            if mode == "elements":
                elements = await _dynamic_discovery(agent)
                return (
                    f"Opened {page.url} (status {status}).{stuck_note}\n"
                    f"Elements: {json.dumps(elements, indent=2)}\n"
                    "Use open_url with type='text' or type='html' for full page content."
                )

            if mode == "html":
                html = await page.content()
                content = html
                title = await page.title() or _extract_html_title(html) or ""
            else:
                # Playwright''s inner_text() reflects computed visibility (display:none,
                # hidden, visually-hidden dialogs, etc.) and pierces shadow DOM natively,
                # unlike _html_to_text(page.content()) which walks raw markup with no
                # concept of what a browser would actually render. This cuts out ad
                # blocks, off-screen login modals, and other DOM-but-not-visible noise
                # that used to leak into "text" mode.
                try:
                    content = await page.locator("body").inner_text(
                        timeout=st["settings"]["timeout_ms"]
                    )
                except Exception:
                    # Fall back to the old path if inner_text() fails for any reason
                    # (e.g. no <body>, detached frame) rather than losing the page.
                    html = await page.content()
                    content = _html_to_text(html, st["settings"]["ignore_tags"])
                title = await page.title() or ""
            content, truncated = _truncate_content(content, st["settings"]["browse_max_chars"])

            suffix     = "\n[truncated]" if truncated else ""
            title_line = f"# {title}\n" if title else ""
            return f"{title_line}{page.url} (status {status}){stuck_note}\n\n{content}{suffix}"

        except Exception as e:
            return f"Error: {e}"

    async def click(target: str, nth: int = 0, exact: bool | None = None) -> str:
        """
        Click an element on the current Camoufox page (whatever open_url loaded last).

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
            return f"Error: {e}"

    async def type_text(
        target: str,
        text: str,
        nth: int = 0,
        exact: bool | None = None,
        clear: bool = True,
    ) -> str:
        """
        Type text into a field on the current Camoufox page (whatever open_url
        loaded last). Append \\n to submit/press Enter.

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
            return f"Error: {e}"

    async def extract_text(target: str = "", nth: int = 0, exact: bool | None = None) -> str:
        """
        Get the visible text content from an element or the whole page.

        Reads the live Camoufox page (whatever open_url loaded last), so it
        reflects any clicks or typing done since. To read a fresh URL, use
        open_url instead.

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
            return f"Error: {e}"

    async def extract_html(target: str = "", nth: int = 0, exact: bool | None = None) -> str:
        """
        Get the HTML markup from an element or the whole page.

        Reads the live Camoufox page (whatever open_url loaded last), so it
        reflects any clicks or typing done since.

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
            return f"Error: {e}"

    async def screenshot_browser(
        target: str | None = None,
        filename: str | None = None,
        nth: int = 0,
        exact: bool | None = None,
        inline: bool = True,
    ) -> str:
        """
        Take a screenshot of the current Camoufox page or a specific element on it.
        Saved to workspace/outputs/browser/<filename>, and returned inline so you
        can see it.

        Use this to capture an element, or the page state after click/type_text.
        To screenshot a URL you have not opened yet, use open_url(type="screenshot").

        Args:
            target: Element to screenshot. Leave empty for the full page.
            filename: Output filename (default: screenshot_<timestamp>.png).
            nth: Which matching element to capture (0 = first).
            exact: Whether text matching must be exact.
            inline: Return the image itself so you can see it (default). Set False
                    to get the saved file path as text instead. Either way the file
                    is written to workspace/outputs/browser/.
        """
        st   = _state(agent)
        page = await _ensure_page(agent)

        path = _screenshot_path(st, filename)
        if isinstance(path, str):
            return path

        try:
            if target:
                loc = await _locate(agent, target, nth=nth, exact=exact)
                await loc.screenshot(path=str(path))
            else:
                await page.screenshot(path=str(path), full_page=True)
            return _image_result(path, inline, st["settings"]["screenshot_max_bytes"])
        except Exception as e:
            return f"Error: {e}"

    async def wait_for(
        target: str,
        state: str = "visible",
        nth: int = 0,
        exact: bool | None = None,
    ) -> str:
        """
        Wait for an element on the current Camoufox page to reach a given state.

        Args:
            target: The element to wait for.
            state: Target state: attached, detached, visible, or hidden.
            nth: Which matching element to watch (0 = first).
            exact: Whether text matching must be exact.
        """
        st = _state(agent)
        try:
            loc = await _locate(agent, target, nth=nth, exact=exact)
            await loc.wait_for(state=state, timeout=st["settings"]["timeout_ms"])  # type: ignore[arg-type]
            return f"Element {target!r} reached state '{state}' (nth={nth})"
        except Exception as e:
            return f"Error: {e}"

    async def manage_browser(action: str, key: str | None = None, value: str | None = None) -> str:
        """
        Manage the Camoufox browser session and settings.

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
            return json.dumps({**st["settings"], "_headless": st.get("_headless", True)}, indent=2)

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
        # http_request removed — deprecated.
        "click":          False,
        "type_text":      False,
        "extract_text":   False,
        "extract_html":   False,
        "screenshot_browser":     False,
        "wait_for":       False,
        "manage_browser": False,
    }

    _WEB_PERMISSIONS: dict[str, int] = {
        "web_search":         25,
        "open_url":           25,
        # http_request removed — deprecated.
        "click":              30,
        "type_text":          30,
        "extract_text":       25,
        "extract_html":       25,
        "screenshot_browser": 25,
        "wait_for":           25,
        "manage_browser":     40,
    }

    for fn in (
        web_search,
        open_url,
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
        agent.tool_handler.register_tool(fn, always_on=always_on, min_permission=_WEB_PERMISSIONS[fn.__name__])
