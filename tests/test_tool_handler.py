"""
tests/test_tool_handler.py

Tests for ToolCallHandler — registration, schema extraction, execution, and
the required_permissions gating seam (docs/PERMISSIONS-PLAN.md §3): static
sets, callable (dynamic) classifiers, override precedence, and
classifier-raises -> deny.

Run with:
    pytest tests/
"""
import pytest
from TinyCTX.tool_handling import ToolCallHandler
from TinyCTX.permissions import Permission
from TinyCTX.utils.bm25 import BM25, _tokenise


class _FakeCaller:
    """Minimal stand-in for the caller object execute_tool_call() now
    requires — needs .username and .effective_permissions(permissions_config),
    mirroring TinyCTX.users.models.User. Defaults to holding every Permission
    (the old _FakeCaller(permission_level=100) equivalent) so tests that
    don't care about gating are unaffected; pass granted_permissions to
    exercise the gate itself."""
    def __init__(self, granted_permissions=None, username: str = "test-caller"):
        self._granted = (
            frozenset(granted_permissions) if granted_permissions is not None
            else frozenset(Permission)
        )
        self.username = username

    def effective_permissions(self, permissions_config=None) -> "frozenset[Permission]":
        return self._granted

    def has_permission(self, perm, permissions_config=None) -> bool:
        return perm in self._granted


# ---------------------------------------------------------------------------
# BM25 unit tests
# ---------------------------------------------------------------------------

class TestTokenise:
    def test_lowercases(self):
        assert _tokenise("Hello World") == ["hello", "world"]

    def test_splits_underscores(self):
        assert _tokenise("web_search") == ["web", "search"]

    def test_splits_hyphens(self):
        assert _tokenise("read-file") == ["read", "file"]

    def test_drops_empty(self):
        assert "" not in _tokenise("  a  b  ")

    def test_empty_string(self):
        assert _tokenise("") == []


class TestBM25:
    def _corpus(self):
        return {
            "shell":      "Run shell commands in the workspace",
            "view":       "Read a file with line numbers or list a directory",
            "web_search": "Search the web for current information",
            "screenshot": "Take a screenshot of the current browser page",
        }

    def test_empty_query_returns_empty(self):
        bm25 = BM25(self._corpus())
        assert bm25.search("") == []

    def test_exact_name_match(self):
        bm25 = BM25(self._corpus())
        hits = bm25.search("shell")
        assert hits[0][0] == "shell"
        assert hits[0][1] > 0.0

    def test_description_match(self):
        bm25 = BM25(self._corpus())
        hits = bm25.search("read file")
        assert hits[0][0] == "view"

    def test_no_match_scores_zero(self):
        bm25 = BM25(self._corpus())
        hits = bm25.search("zzznomatch")
        assert all(score == 0.0 for _, score in hits)

    def test_results_sorted_descending(self):
        bm25 = BM25(self._corpus())
        hits = bm25.search("search web information")
        scores = [s for _, s in hits]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_limits_results(self):
        bm25 = BM25(self._corpus())
        hits = bm25.search("the", top_k=2)
        assert len(hits) <= 2

    def test_underscore_split_enables_partial_name_match(self):
        """Query 'search' should hit web_search because it tokenises as ['web','search']."""
        bm25 = BM25(self._corpus())
        hit_names = {name for name, score in bm25.search("search") if score > 0}
        assert "web_search" in hit_names

    def test_case_insensitive(self):
        bm25 = BM25(self._corpus())
        lower = bm25.search("screenshot")
        upper = bm25.search("SCREENSHOT")
        assert lower[0][0] == upper[0][0]
        assert lower[0][1] == pytest.approx(upper[0][1])


# ---------------------------------------------------------------------------
# Registration and schema extraction
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def setup_method(self):
        self.handler = ToolCallHandler()

    def test_register_simple_function(self):
        def greet(name: str) -> str:
            """Say hello to someone.

            Args:
                name: The person's name.
            """
            return f"Hello, {name}!"

        self.handler.register_tool(greet)
        assert "greet" in self.handler.tools

    def test_description_extracted_from_docstring(self):
        def greet(name: str) -> str:
            """Say hello to someone.

            Args:
                name: The person's name.
            """
            return f"Hello, {name}!"

        self.handler.register_tool(greet)
        assert self.handler.tools["greet"]["description"] == "Say hello to someone."

    def test_arg_description_extracted(self):
        def greet(name: str) -> str:
            """Say hello.

            Args:
                name: The person's name.
            """
            return f"Hello, {name}!"

        self.handler.register_tool(greet)
        assert "description" in self.handler.tools["greet"]["properties"]["name"]
        assert "name" in self.handler.tools["greet"]["properties"]["name"]["description"].lower()

    def test_required_args_captured(self):
        def fn(required_arg: str, optional_arg: str = "default") -> str:
            """A function."""
            return required_arg

        self.handler.register_tool(fn)
        tool = self.handler.tools["fn"]
        assert "required_arg" in tool["required"]
        assert "optional_arg" not in tool["required"]

    def test_type_annotations_mapped(self):
        def fn(s: str, i: int, f: float, b: bool, d: dict, lst: list) -> str:
            """Types test."""
            return ""

        self.handler.register_tool(fn)
        props = self.handler.tools["fn"]["properties"]
        assert props["s"]["type"] == "string"
        assert props["i"]["type"] == "integer"
        assert props["f"]["type"] == "number"
        assert props["b"]["type"] == "boolean"
        assert props["d"]["type"] == "object"
        assert props["lst"]["type"] == "array"

    def test_no_docstring_falls_back_gracefully(self):
        def nodoc(x: str) -> str:
            return x

        self.handler.register_tool(nodoc)
        assert "nodoc" in self.handler.tools
        assert self.handler.tools["nodoc"]["description"]  # not empty

    def test_custom_name_override(self):
        def fn() -> str:
            """Does something."""
            return ""

        self.handler.register_tool(fn, name="custom_name")
        assert "custom_name" in self.handler.tools
        assert "fn" not in self.handler.tools

    def test_custom_description_override(self):
        def fn() -> str:
            """Original docstring."""
            return ""

        self.handler.register_tool(fn, description="My custom description")
        assert self.handler.tools["fn"]["description"] == "My custom description"


# ---------------------------------------------------------------------------
# always_on / deferred registration
# ---------------------------------------------------------------------------

class TestAlwaysOnDeferred:
    def setup_method(self):
        self.handler = ToolCallHandler()

    def test_always_on_immediately_enabled(self):
        def fn() -> str:
            """Always on."""
            return ""
        self.handler.register_tool(fn, always_on=True)
        assert "fn" in self.handler.enabled

    def test_deferred_not_in_enabled(self):
        def fn() -> str:
            """Deferred."""
            return ""
        self.handler.register_tool(fn)  # always_on defaults to False
        assert "fn" not in self.handler.enabled

    def test_deferred_still_in_tools(self):
        """Deferred tools are registered in self.tools even though not enabled."""
        def fn() -> str:
            """Deferred."""
            return ""
        self.handler.register_tool(fn)
        assert "fn" in self.handler.tools

    def test_enable_method(self):
        def fn() -> str:
            """Tool."""
            return ""
        self.handler.register_tool(fn)
        assert "fn" not in self.handler.enabled
        result = self.handler.enable("fn")
        assert result is True
        assert "fn" in self.handler.enabled

    def test_enable_unknown_returns_false(self):
        assert self.handler.enable("nonexistent") is False


# ---------------------------------------------------------------------------
# tools_search() — BM25-backed
# ---------------------------------------------------------------------------

class TestToolsSearch:
    def setup_method(self):
        self.handler = ToolCallHandler()
        # Register tools_search itself as always_on (mirrors agent.py bootstrap)
        self.handler.register_tool(self.handler.tools_search, always_on=True)

    def _add(self, name: str, description: str, always_on: bool = False):
        """Helper: register an anonymous tool with given name and description."""
        def fn() -> str:
            return ""
        fn.__name__ = name
        fn.__doc__ = description
        self.handler.register_tool(fn, always_on=always_on)

    async def test_search_lists_matching_tool_as_candidate(self):
        """Fuzzy match doesn't enable — it lists the tool as a candidate to
        re-search for by exact name (see tools_search docstring)."""
        self._add("web_search", "Search the web for information")
        result = await self.handler.tools_search("web search")
        assert "web_search" not in self.handler.enabled
        assert "web_search" in result

    async def test_search_exact_name_enables_immediately(self):
        """An exact tool-name match is a one-step enable, unlike a fuzzy query."""
        self._add("web_search", "Search the web for information")
        result = await self.handler.tools_search("web_search")
        assert "web_search" in self.handler.enabled
        assert "Enabled" in result

    async def test_search_matches_description(self):
        self._add("fetch_page", "Download and return the HTML of a URL")
        result = await self.handler.tools_search("HTML")
        assert "fetch_page" not in self.handler.enabled
        assert "fetch_page" in result

    async def test_search_case_insensitive(self):
        """BM25 tokeniser lowercases everything so queries are case-insensitive."""
        self._add("screenshot", "Take a screenshot of the page")
        result = await self.handler.tools_search("SCREENSHOT")
        assert "screenshot" not in self.handler.enabled
        assert "screenshot" in result

    async def test_search_no_match_returns_message(self):
        """A query with no BM25-positive matches returns a no-results message."""
        self._add("shell", "Run shell commands in the workspace")
        result = await self.handler.tools_search("zzznomatch")
        assert "No" in result or "no" in result
        assert "shell" not in self.handler.enabled

    async def test_search_skips_already_enabled(self):
        self._add("already", "Already enabled unique tool", always_on=True)
        result = await self.handler.tools_search("already enabled")
        # Should not re-add, should report it's already enabled
        assert "already" in result.lower()
        assert "No new" in result or "Already" in result

    async def test_search_ranks_best_match_first(self):
        """The tool most relevant to the query should be listed first among candidates."""
        self._add("read_file", "Read the contents of a file from disk")
        self._add("view_file", "View a file with line numbers")
        result = await self.handler.tools_search("read file")
        assert "read_file" not in self.handler.enabled
        assert result.index("read_file") < result.index("view_file")

    async def test_search_multiple_matches(self):
        """Both tools sharing query terms should be listed as candidates."""
        self._add("click", "Click an element on the page")
        self._add("double_click", "Double click an element on the page")
        result = await self.handler.tools_search("click element")
        assert "click" not in self.handler.enabled
        assert "double_click" not in self.handler.enabled
        assert "click" in result
        assert "double_click" in result

    async def test_underscore_names_matched_as_words(self):
        """web_search is tokenised as ['web', 'search'] so query 'search' hits it."""
        self._add("web_search", "Search the web for current information")
        self._add("view", "Read a file with line numbers")
        result = await self.handler.tools_search("search")
        assert "web_search" not in self.handler.enabled
        assert "web_search" in result
        assert "view" not in result

    def test_tools_search_itself_always_on(self):
        """tools_search should always be in the tool definitions."""
        defs = self.handler.get_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "tools_search" in names


# ---------------------------------------------------------------------------
# get_tool_definitions() — now filters to enabled set
# ---------------------------------------------------------------------------

class TestToolDefinitions:
    def setup_method(self):
        self.handler = ToolCallHandler()

    def test_definitions_format(self):
        def search(query: str) -> str:
            """Search the web.

            Args:
                query: What to search for.
            """
            return ""

        self.handler.register_tool(search, always_on=True)
        defs = self.handler.get_tool_definitions()

        assert len(defs) == 1
        d = defs[0]
        assert d["type"] == "function"
        assert d["function"]["name"] == "search"
        assert "description" in d["function"]
        assert d["function"]["parameters"]["type"] == "object"
        assert "query" in d["function"]["parameters"]["properties"]

    def test_empty_handler_returns_empty_list(self):
        assert self.handler.get_tool_definitions() == []

    def test_deferred_tools_not_in_definitions(self):
        """Deferred (not always_on) tools must not appear in definitions."""
        def secret() -> str:
            """A deferred tool."""
            return ""
        self.handler.register_tool(secret)  # deferred
        assert self.handler.get_tool_definitions() == []

    def test_only_enabled_tools_returned(self):
        def a() -> str:
            """A."""
            return ""
        def b() -> str:
            """B."""
            return ""
        def c() -> str:
            """C — deferred."""
            return ""

        self.handler.register_tool(a, always_on=True)
        self.handler.register_tool(b, always_on=True)
        self.handler.register_tool(c)  # deferred
        defs = self.handler.get_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        assert names == {"a", "b"}
        assert "c" not in names

    def test_enable_then_appears_in_definitions(self):
        """Enabling a deferred tool mid-session makes it appear in next definitions call."""
        def lazy() -> str:
            """Lazy tool."""
            return ""
        self.handler.register_tool(lazy)
        assert self.handler.get_tool_definitions() == []
        self.handler.enable("lazy")
        defs = self.handler.get_tool_definitions()
        assert any(d["function"]["name"] == "lazy" for d in defs)


# ---------------------------------------------------------------------------
# execute_tool_call() — sync functions
# ---------------------------------------------------------------------------

class TestExecuteToolSync:
    def setup_method(self):
        self.handler = ToolCallHandler()

    @pytest.mark.asyncio
    async def test_execute_returns_result(self):
        def add(a: int, b: int) -> str:
            """Add two numbers."""
            return str(a + b)

        # execute_tool_call now requires the tool to be in self.enabled (not
        # just self.tools), so register it as always_on.
        self.handler.register_tool(add, always_on=True)
        result = await self.handler.execute_tool_call({
            "id": "call1",
            "function": {"name": "add", "arguments": '{"a": 3, "b": 4}'}
        }, _FakeCaller())
        assert result["success"] is True
        assert result["result"] == "7"

    @pytest.mark.asyncio
    async def test_execute_deferred_tool_not_callable(self):
        """A deferred (not enabled) tool is rejected until enabled — this is
        the current permission model: execute_tool_call checks self.enabled,
        not just self.tools."""
        def multiply(a: int, b: int) -> str:
            """Multiply."""
            return str(a * b)

        self.handler.register_tool(multiply)  # deferred — not in enabled
        assert "multiply" not in self.handler.enabled
        result = await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "multiply", "arguments": '{"a": 6, "b": 7}'}
        }, _FakeCaller())
        assert result["success"] is False
        assert "not found" in result["error"]

        # Once enabled, the same call succeeds.
        self.handler.enable("multiply")
        result = await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "multiply", "arguments": '{"a": 6, "b": 7}'}
        }, _FakeCaller())
        assert result["success"] is True
        assert result["result"] == "42"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_returns_error(self):
        result = await self.handler.execute_tool_call({
            "id": "call1",
            "function": {"name": "nonexistent", "arguments": "{}"}
        }, _FakeCaller())
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_invalid_json_returns_error(self):
        def fn(x: str) -> str:
            """A function."""
            return x

        self.handler.register_tool(fn, always_on=True)
        result = await self.handler.execute_tool_call({
            "id": "call1",
            "function": {"name": "fn", "arguments": "not valid json {{{"}
        }, _FakeCaller())
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_execute_dict_args(self):
        """Arguments can be passed as a dict instead of a JSON string."""
        def greet(name: str) -> str:
            """Greet."""
            return f"hi {name}"

        self.handler.register_tool(greet, always_on=True)
        result = await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "greet", "arguments": {"name": "world"}}
        }, _FakeCaller())
        assert result["success"] is True
        assert result["result"] == "hi world"

    @pytest.mark.asyncio
    async def test_execute_raises_captured_as_error(self):
        def boom(x: str) -> str:
            """Explodes."""
            raise ValueError("intentional failure")

        self.handler.register_tool(boom, always_on=True)
        result = await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "boom", "arguments": '{"x": "test"}'}
        }, _FakeCaller())
        assert result["success"] is False
        assert "intentional failure" in result["error"]


# ---------------------------------------------------------------------------
# execute_tool_call() — async functions
# ---------------------------------------------------------------------------

class TestExecuteToolAsync:
    def setup_method(self):
        self.handler = ToolCallHandler()

    @pytest.mark.asyncio
    async def test_async_tool_awaited(self):
        import asyncio

        async def slow_add(a: int, b: int) -> str:
            """Async add."""
            await asyncio.sleep(0)
            return str(a + b)

        self.handler.register_tool(slow_add, always_on=True)
        result = await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "slow_add", "arguments": '{"a": 10, "b": 5}'}
        }, _FakeCaller())
        assert result["success"] is True
        assert result["result"] == "15"

    @pytest.mark.asyncio
    async def test_async_tool_exception_captured(self):
        async def async_boom(x: str) -> str:
            """Async explodes."""
            raise RuntimeError("async failure")

        self.handler.register_tool(async_boom, always_on=True)
        result = await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "async_boom", "arguments": '{"x": "hi"}'}
        }, _FakeCaller())
        assert result["success"] is False
        assert "async failure" in result["error"]


# ---------------------------------------------------------------------------
# required_permissions — static set (docs/PERMISSIONS-PLAN.md §3)
# ---------------------------------------------------------------------------

class TestRequiredPermissionsStatic:
    def setup_method(self):
        self.handler = ToolCallHandler()

    async def _call(self, name: str, caller, args=None):
        return await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": name, "arguments": args or {}},
        }, caller)

    @pytest.mark.asyncio
    async def test_caller_with_required_permission_succeeds(self):
        def read_thing() -> str:
            """Reads something."""
            return "ok"

        self.handler.register_tool(
            read_thing, always_on=True, required_permissions={Permission.FILE_READ},
        )
        caller = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await self._call("read_thing", caller)
        assert result["success"] is True
        assert result["result"] == "ok"

    @pytest.mark.asyncio
    async def test_caller_missing_required_permission_denied(self):
        def write_thing() -> str:
            """Writes something."""
            return "ok"

        self.handler.register_tool(
            write_thing, always_on=True, required_permissions={Permission.FILE_WRITE},
        )
        caller = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await self._call("write_thing", caller)
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]
        assert "file_write" in result["error"]

    @pytest.mark.asyncio
    async def test_required_permissions_none_is_ungated(self):
        """required_permissions=None must be usable regardless of what the
        caller holds — an explicitly ungated tool, not one requiring the
        empty set of a mis-specified gate."""
        def anyone_can_call() -> str:
            """Ungated."""
            return "ok"

        self.handler.register_tool(
            anyone_can_call, always_on=True, required_permissions=None,
        )
        caller = _FakeCaller(granted_permissions=set())  # holds nothing
        result = await self._call("anyone_can_call", caller)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_multiple_required_permissions_all_must_be_held(self):
        def both() -> str:
            """Needs two."""
            return "ok"

        self.handler.register_tool(
            both, always_on=True,
            required_permissions={Permission.FILE_READ, Permission.FILE_WRITE},
        )
        partial_caller = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await self._call("both", partial_caller)
        assert result["success"] is False

        full_caller = _FakeCaller(granted_permissions={Permission.FILE_READ, Permission.FILE_WRITE})
        result = await self._call("both", full_caller)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_network_write_implies_network_read_requirement(self):
        """expand() applies implications to the REQUIREMENT — a tool
        declaring NETWORK_WRITE actually demands both bools of the caller
        (docs/PERMISSIONS-PLAN.md §1.2)."""
        def send() -> str:
            """Sends data."""
            return "ok"

        self.handler.register_tool(
            send, always_on=True, required_permissions={Permission.NETWORK_WRITE},
        )
        # Holds NETWORK_WRITE but NOT NETWORK_READ — must still be denied,
        # since expand() makes the requirement include NETWORK_READ too.
        caller = _FakeCaller(granted_permissions={Permission.NETWORK_WRITE})
        result = await self._call("send", caller)
        assert result["success"] is False

        full_caller = _FakeCaller(granted_permissions={Permission.NETWORK_WRITE, Permission.NETWORK_READ})
        result = await self._call("send", full_caller)
        assert result["success"] is True


# ---------------------------------------------------------------------------
# required_permissions — callable (dynamic classifier)
# ---------------------------------------------------------------------------

class TestRequiredPermissionsCallable:
    def setup_method(self):
        self.handler = ToolCallHandler()

    @pytest.mark.asyncio
    async def test_callable_receives_coerced_kwargs(self):
        """The classifier must see the call's own (coerced) arguments, not
        just a fixed set — e.g. shell's/present's per-argument classifiers."""
        seen_args = {}

        def classify(path: str, **_ignored):
            seen_args["path"] = path
            if path.startswith("/system/"):
                return {Permission.ROOT}
            return {Permission.FILE_READ}

        def deliver(path: str) -> str:
            """Deliver a file."""
            return f"delivered {path}"

        self.handler.register_tool(deliver, always_on=True, required_permissions=classify)

        reader = _FakeCaller(granted_permissions={Permission.FILE_READ})
        result = await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "deliver", "arguments": {"path": "/workspace/notes.md"}},
        }, reader)
        assert result["success"] is True
        assert seen_args["path"] == "/workspace/notes.md"

        # Same tool, different argument -> different requirement (ROOT) ->
        # the same FILE_READ-only caller is now denied.
        result = await self.handler.execute_tool_call({
            "id": "c2",
            "function": {"name": "deliver", "arguments": {"path": "/system/SOUL.md"}},
        }, reader)
        assert result["success"] is False

        root_caller = _FakeCaller(granted_permissions={Permission.ROOT})
        result = await self.handler.execute_tool_call({
            "id": "c3",
            "function": {"name": "deliver", "arguments": {"path": "/system/SOUL.md"}},
        }, root_caller)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_classifier_raises_denies_the_call(self):
        """A required_permissions callable that raises must DENY, not crash
        the caller or (worse) fail open."""
        def broken_classifier(**_ignored):
            raise RuntimeError("classifier bug")

        def risky() -> str:
            """Risky tool."""
            return "should never run"

        self.handler.register_tool(risky, always_on=True, required_permissions=broken_classifier)
        caller = _FakeCaller()  # holds every Permission
        result = await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "risky", "arguments": {}},
        }, caller)
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"] or "could not classify" in result["error"]

    @pytest.mark.asyncio
    async def test_coercion_happens_before_classifier_sees_args(self):
        """backend_access=True serialized as the string 'true' by a
        forgetful LLM must reach the classifier as an actual bool, not the
        literal string — see handler.py's _coerce_args ordering comment."""
        seen = {}

        def classify(flag: bool = False, **_ignored):
            seen["flag"] = flag
            seen["flag_type"] = type(flag)
            return set()

        def fn(flag: bool = False) -> str:
            """Takes a bool."""
            return "ok"

        self.handler.register_tool(fn, always_on=True, required_permissions=classify)
        caller = _FakeCaller()
        await self.handler.execute_tool_call({
            "id": "c1",
            "function": {"name": "fn", "arguments": '{"flag": "true"}'},
        }, caller)
        assert seen["flag"] is True
        assert seen["flag_type"] is bool


# ---------------------------------------------------------------------------
# assert_permissions_declared() — forgotten declaration is a bug, not an
# ungated tool (docs/PERMISSIONS-PLAN.md §3)
# ---------------------------------------------------------------------------

class TestAssertPermissionsDeclared:
    def setup_method(self):
        self.handler = ToolCallHandler()

    def test_forgotten_declaration_trips_assertion(self):
        def fn() -> str:
            """No required_permissions passed at all."""
            return "ok"

        self.handler.register_tool(fn, always_on=True)  # forgot required_permissions
        with pytest.raises(RuntimeError):
            self.handler.assert_permissions_declared()

    def test_explicit_none_does_not_trip_assertion(self):
        def fn() -> str:
            """Deliberately ungated."""
            return "ok"

        self.handler.register_tool(fn, always_on=True, required_permissions=None)
        self.handler.assert_permissions_declared()  # must not raise

    def test_explicit_set_does_not_trip_assertion(self):
        def fn() -> str:
            """Gated."""
            return "ok"

        self.handler.register_tool(fn, always_on=True, required_permissions={Permission.FILE_READ})
        self.handler.assert_permissions_declared()  # must not raise

    def test_callable_does_not_trip_assertion(self):
        def fn(**_ignored) -> str:
            """Dynamically gated."""
            return "ok"

        self.handler.register_tool(
            fn, always_on=True, required_permissions=lambda **_: {Permission.FILE_READ},
        )
        self.handler.assert_permissions_declared()  # must not raise

    def test_assertion_names_every_undeclared_tool(self):
        def a() -> str:
            """A."""
            return ""
        def b() -> str:
            """B."""
            return ""

        self.handler.register_tool(a, always_on=True)
        self.handler.register_tool(b, always_on=True)
        with pytest.raises(RuntimeError) as excinfo:
            self.handler.assert_permissions_declared()
        assert "a" in str(excinfo.value)
        assert "b" in str(excinfo.value)


# ---------------------------------------------------------------------------
# apply_overrides() — always_on precedence, and the deprecated
# min_permission field (warn + ignore, not silently honored)
# ---------------------------------------------------------------------------

class TestApplyOverridesPrecedence:
    def setup_method(self):
        self.handler = ToolCallHandler()

    def test_always_on_true_override_enables_deferred_tool(self):
        def fn() -> str:
            """Deferred by default."""
            return ""
        self.handler.register_tool(fn, always_on=False, required_permissions=None)
        assert "fn" not in self.handler.enabled

        from TinyCTX.config import ToolOverrideConfig
        self.handler.apply_overrides({"fn": ToolOverrideConfig(always_on=True)})
        assert "fn" in self.handler.enabled

    def test_always_on_false_override_disables_always_on_tool(self):
        def fn() -> str:
            """Always on by default."""
            return ""
        self.handler.register_tool(fn, always_on=True, required_permissions=None)
        assert "fn" in self.handler.enabled

        from TinyCTX.config import ToolOverrideConfig
        self.handler.apply_overrides({"fn": ToolOverrideConfig(always_on=False)})
        assert "fn" not in self.handler.enabled

    def test_min_permission_override_is_ignored_not_fatal(self, caplog):
        """A stale config still naming min_permission must not error, and
        must not affect who can call the tool — the value is IGNORED, with
        a loud warning, per handler.py's apply_overrides docstring."""
        def fn() -> str:
            """Gated tool."""
            return "ok"
        self.handler.register_tool(fn, always_on=True, required_permissions={Permission.ROOT})

        from TinyCTX.config import ToolOverrideConfig
        import logging
        with caplog.at_level(logging.WARNING):
            self.handler.apply_overrides({"fn": ToolOverrideConfig(min_permission=90)})
        assert any("min_permission" in rec.message for rec in caplog.records)
        # Still gated on ROOT — the override did not weaken the real check.
        assert self.handler.tools["fn"]["required_permissions"] is not None

    def test_override_for_unknown_tool_is_skipped(self):
        """Overriding a tool name that was never registered must not raise
        — not every module is loaded in every config."""
        from TinyCTX.config import ToolOverrideConfig
        self.handler.apply_overrides({"never_registered": ToolOverrideConfig(always_on=True)})  # no raise