import asyncio
import functools
import inspect
import json
import logging
from typing import Dict, Any, Callable, List, Optional

logger = logging.getLogger(__name__)

class ToolCallHandler:
    def __init__(self, vector_store=None, embedder=None):
        """
        vector_store: TinyCTX.tool_handling.vector_store.ToolVectorStore | None.
            Process-wide singleton owned by Runtime (built once, reused across
            every AgentCycle's ToolCallHandler — see runtime.py). None disables
            both passive auto-enable and vector-assisted tools_search; the
            handler still works exactly as before (BM25-only, explicit
            tools_search) when vector_store is absent.
        embedder: TinyCTX.ai.Embedder | None. Also process-wide, built once
            alongside vector_store from config.tools.passive/search's
            embedding_model. None means vector ranking is skipped even if
            vector_store is present (BM25/FTS still works without an embedder).
        """
        self.tools: Dict[str, Any] = {}
        self.enabled: set[str] = set()
        self.vector_store = vector_store
        self.embedder = embedder
        # ToolSearchConfig instance, set by agent.py after construction. Read
        # by _search_cfg() / tools_search(); left None in bare/test usage,
        # where _search_cfg()'s defaults apply.
        self.search_config = None
        # Tool names auto-enabled by the current turn's passive_search() call —
        # NOT part of `enabled` persistence; agent.py unions this set into
        # get_tool_definitions() for the turn and never writes it to session
        # state. Reset at the start of every passive_search() call.
        self.auto_enabled: set[str] = set()

    def register_tool(self,
                      func: Callable,
                      name: Optional[str] = None,
                      description: Optional[str] = None,
                      always_on: bool = False,
                      min_permission: int = 25):
        """Register a tool function - auto-extracts name and description if not provided
        
        Args:
            func: The function to register
            name: Tool name (defaults to function name)
            description: Tool description (defaults to first paragraph of docstring)
        """
        if name is None:
            name = func.__name__
        
        if description is None:
            description, arg_descs = self._extract_docstring_parts(func)
        else:
            arg_descs = {}
        
        sig = inspect.signature(func)
        properties = {}
        required = []
        
        for param_name, param in sig.parameters.items():
            param_type = self._python_type_to_json_schema(param.annotation) if param.annotation != inspect.Parameter.empty else {"type": "string"}
            param_description = arg_descs.get(param_name, "")
            properties[param_name] = {**param_type}
            if param_description:
                properties[param_name]["description"] = param_description
            if param.default == inspect.Parameter.empty:
                required.append(param_name)
        
        self.tools[name] = {
            'function': func,
            'description': description,
            'signature': sig,
            'properties': properties,
            'required': required,
            'min_permission': min_permission,
        }
        if always_on:
            self.enabled.add(name)

    def _extract_docstring_parts(self, func: Callable) -> tuple[str, Dict[str, str]]:
        """
        Extracts:
         - main description (the docstring text before Args/Parameters)
         - argument descriptions (dict mapping arg name -> description)
        """
        doc = func.__doc__
        if not doc:
            return (f"Function: {func.__name__}", {})

        lines = doc.strip().splitlines()
        arg_descs = {}
        in_args = False
        main_line = None

        for line in lines:
            stripped = line.strip()
            if stripped in ("Args:", "Parameters:"):
                in_args = True
                continue
            if stripped in ("Returns:", "Raises:"):
                in_args = False
            if in_args and ":" in stripped:
                param, desc = stripped.split(":", 1)
                arg_descs[param.strip().split()[0]] = desc.strip()
            elif not in_args and not main_line and stripped and not stripped.startswith(("Args:", "Parameters:", "Returns:", "Raises:")):
                main_line = stripped
        return main_line or f"Function: {func.__name__}", arg_descs

    def _python_type_to_json_schema(self, annotation) -> Dict[str, Any]:
        import typing
        import types

        # `from __future__ import annotations` makes all annotations lazy strings.
        # Resolve them back to actual types before anything else.
        if isinstance(annotation, str):
            resolved = {
                'int': int, 'float': float, 'bool': bool,
                'str': str, 'dict': dict, 'list': list,
            }.get(annotation)
            if resolved is not None:
                annotation = resolved
            else:
                return {"type": "string"}

        origin = getattr(annotation, '__origin__', None)
        args   = getattr(annotation, '__args__', None)

        # Handle Python 3.10+ `X | Y` union syntax (types.UnionType)
        if isinstance(annotation, types.UnionType):
            non_none = [a for a in annotation.__args__ if a is not type(None)]
            if len(non_none) == 1:
                return self._python_type_to_json_schema(non_none[0])
            return {"type": "string"}

        # list[X] — also catches bare `list` resolved from a stringified annotation
        # (from __future__ import annotations turns `list[str]` into the string
        # 'list[str]', which the string-resolution block above can't parse, but
        # bare `list` resolves to the list builtin which has no __origin__.
        # Both cases must produce {"type": "array", "items": ...} — never a bare
        # {"type": "array"} without items, which causes backend Jinja2 to crash.)
        if origin is list or annotation is list:
            item_schema = self._python_type_to_json_schema(args[0]) if args else {"type": "string"}
            return {"type": "array", "items": item_schema}

        # tuple[X, ...]
        if origin is tuple:
            return {"type": "array"}

        # dict[K, V]
        if origin is dict:
            return {"type": "object"}

        # Optional[X] == Union[X, None]
        if origin is typing.Union:
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                return self._python_type_to_json_schema(non_none[0])
            return {"type": "string"}

        # Literal["a", "b"]
        if origin is typing.Literal:
            return {"type": "string", "enum": list(args)}

        mapping = {
            str:   {"type": "string"},
            int:   {"type": "integer"},
            float: {"type": "number"},
            bool:  {"type": "boolean"},
            dict:  {"type": "object"},
        }
        return mapping.get(annotation, {"type": "string"})

    def _coerce_args(self, function_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce argument values to their declared Python types.

        JSON deserialization loses type information (e.g. the LLM may emit
        ``{"level": "5"}`` even though the schema says integer). We walk the
        registered signature and attempt a safe cast for mismatched primitives.
        Non-primitive or already-correct values are left untouched.
        """
        sig = self.tools[function_name]['signature']
        coerced = {}
        for key, value in args.items():
            param = sig.parameters.get(key)
            if param is None or param.annotation is inspect.Parameter.empty:
                coerced[key] = value
                continue
            ann = param.annotation
            # Unwrap Optional[X] / X | None
            import types as _types, typing as _typing
            # Resolve stringified annotations from `from __future__ import annotations`
            if isinstance(ann, str):
                ann = {'int': int, 'float': float, 'bool': bool, 'str': str}.get(ann, ann)
            origin = getattr(ann, '__origin__', None)
            type_args = getattr(ann, '__args__', None)
            if isinstance(ann, _types.UnionType) or origin is _typing.Union:
                non_none = [a for a in (ann.__args__ if hasattr(ann, '__args__') else type_args) if a is not type(None)]
                ann = non_none[0] if len(non_none) == 1 else ann
            if ann in (int, float, bool, str) and not isinstance(value, ann):
                try:
                    if ann is bool:
                        # bool("false") == True in Python — handle string literals explicitly
                        if isinstance(value, str):
                            coerced[key] = value.strip().lower() not in ("false", "0", "no", "")
                        else:
                            coerced[key] = bool(value)
                    else:
                        coerced[key] = ann(value)
                except (ValueError, TypeError):
                    coerced[key] = value  # leave it; let the function raise
            else:
                coerced[key] = value
        return coerced

    def apply_overrides(self, overrides: Dict[str, Any]) -> None:
        """Apply config-driven per-tool overrides of always_on / min_permission.

        Call after all modules have registered their tools (register_tool
        calls set the baseline; this runs last and wins). Unset fields on an
        override (None) leave that aspect of the tool untouched. Tool names
        not currently registered are skipped with a debug log — not every
        module is loaded in every config, so this isn't an error.
        """
        for name, override in (overrides or {}).items():
            tool = self.tools.get(name)
            if tool is None:
                logger.debug("[tool_handler] override for unknown tool '%s' skipped", name)
                continue

            min_permission = getattr(override, "min_permission", None)
            if min_permission is not None:
                tool["min_permission"] = min_permission

            always_on = getattr(override, "always_on", None)
            if always_on is True:
                self.enabled.add(name)
            elif always_on is False:
                self.enabled.discard(name)

    def enable(self, name: str) -> bool:
        """Enable a registered tool by name. Returns True if found, False if unknown."""
        if name not in self.tools:
            return False
        self.enabled.add(name)
        return True

    async def tools_search(self, query: str) -> str:
        """Search for available tools by keyword and enable them for use. If the query exactly matches a tool name, that tool is enabled immediately.
        Otherwise, returns a list of candidates — call tools_search again with the
        exact tool name you want to enable.

        Args:
            query: Exact tool name to enable, or a keyword/description to search for.
        """
        # --- Exact match: enable immediately ---
        if query in self.tools:
            if query in self.enabled:
                return f"'{query}' is already enabled."
            self.enabled.add(query)
            return f"Enabled: {query}"

        # --- Fuzzy search: suggest candidates, do NOT enable anything ---
        if not self.tools:
            return "No tools available."

        hits = await self._ranked_candidates(
            query,
            vector_enabled=self._search_cfg("vector_enabled", False),
            top_k=self._search_cfg("top_k", 5),
            min_score=self._search_cfg("min_score", 0.0),
            rrf_k=self._search_cfg("rrf_k", 60),
        )

        if not hits:
            return "No tools found matching that query."

        lines = [f"No exact match for '{query}'. Call tools_search again with the exact name you want to enable. Candidates:"]
        for name in hits:
            desc = self.tools[name]['description']
            already = " (already enabled)" if name in self.enabled else ""
            lines.append(f"  - {name}{already}: {desc}")
        return "\n".join(lines)

    def _search_cfg(self, key: str, default):
        """Read one field from the ToolSearchConfig this handler was
        configured with, or fall back to `default` when unset (no config
        wired — e.g. in tests constructing ToolCallHandler bare)."""
        cfg = getattr(self, "search_config", None)
        return getattr(cfg, key, default) if cfg is not None else default

    async def _ranked_candidates(
        self, query: str, *, vector_enabled: bool, top_k: int, min_score: float, rrf_k: int,
    ) -> List[str]:
        """Shared entry into tool_handling/search.py's rank_tools — used by
        both tools_search (above) and passive_search (below). Returns []
        (BM25/vector both no-op) when vector_store is absent, in which case
        callers should fall back to plain BM25 via utils.bm25 directly."""
        if self.vector_store is None:
            from TinyCTX.utils.bm25 import BM25
            corpus = {
                name: f"{name.replace('_', ' ')} {tool['description']}"
                for name, tool in self.tools.items()
            }
            scored = BM25(corpus).search(query, top_k=top_k)
            return [name for name, score in scored if score >= min_score]

        from TinyCTX.tool_handling.search import sync_store, rank_tools
        embedding_model = self._search_cfg("embedding_model", "")
        await sync_store(self.vector_store, self.tools, self.embedder if vector_enabled else None, embedding_model)
        return await rank_tools(
            query,
            self.tools,
            self.vector_store,
            embedder=self.embedder,
            embedding_model=embedding_model,
            vector_enabled=vector_enabled,
            top_k=top_k,
            min_score=min_score,
            rrf_k=rrf_k,
        )

    async def passive_search(self, query: str, passive_config) -> set[str]:
        """
        Automatic, un-requested tool enabling — run once per user turn by
        agent.py, before the model's tool list is assembled. Ranks the full
        registry against `query` (the incoming user message) and populates
        self.auto_enabled with up to passive_config.auto_limit hits scoring
        >= passive_config.auto_min_score.

        Does NOT touch self.enabled — the caller (agent.py) is responsible
        for unioning self.auto_enabled into get_tool_definitions() for this
        turn only and never persisting it to session state. Resets
        self.auto_enabled on every call, so calling this again (e.g. on a
        later turn cycle) replaces rather than accumulates.

        No-ops (returns empty set) when passive_config disables both
        auto_bm25_enabled and auto_vector_enabled, or when the registry is
        empty.
        """
        self.auto_enabled = set()
        if not self.tools:
            return self.auto_enabled

        bm25_on = getattr(passive_config, "auto_bm25_enabled", True)
        vec_on  = getattr(passive_config, "auto_vector_enabled", False)
        if not bm25_on and not vec_on:
            return self.auto_enabled

        limit     = getattr(passive_config, "auto_limit", 2)
        min_score = getattr(passive_config, "auto_min_score", 0.0)
        rrf_k     = getattr(passive_config, "rrf_k", 60)

        if self.vector_store is None or not bm25_on:
            # No cache-backed store, or BM25-only requested: plain BM25 pass,
            # no sync/embed cost.
            from TinyCTX.utils.bm25 import BM25
            corpus = {
                name: f"{name.replace('_', ' ')} {tool['description']}"
                for name, tool in self.tools.items()
            }
            scored = BM25(corpus).search(query, top_k=limit)
            hits = [name for name, score in scored if score >= min_score]
        else:
            from TinyCTX.tool_handling.search import sync_store, rank_tools
            embedding_model = getattr(passive_config, "embedding_model", "")
            await sync_store(self.vector_store, self.tools, self.embedder if vec_on else None, embedding_model)
            hits = await rank_tools(
                query,
                self.tools,
                self.vector_store,
                embedder=self.embedder,
                embedding_model=embedding_model,
                vector_enabled=vec_on,
                top_k=limit,
                min_score=min_score,
                rrf_k=rrf_k,
            )

        self.auto_enabled = set(hits)
        return self.auto_enabled

    def get_tool_definitions(self, caller_level: int = 100, minimal_tokens: bool = False) -> List[Dict[str, Any]]:
        definitions = []
        for name in self.enabled | self.auto_enabled:
            tool = self.tools.get(name)
            if tool is None:
                continue
            if minimal_tokens and caller_level < tool.get('min_permission', 25):
                continue  # silently excluded — LLM never sees this tool
            definitions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool['description'],
                    "parameters": {
                        "type": "object",
                        "properties": tool['properties'],
                        "required": tool['required']
                    }
                }
            })
        return definitions
    
    async def execute_tool_call(self, tool_call, caller) -> Dict[str, Any]:
        """Execute a tool call from the LLM."""
        caller_level    = caller.permission_level
        caller_username = caller.username
        try:
            if hasattr(tool_call, 'function'):
                function_name = tool_call.function.name
                arguments = tool_call.function.arguments
                tool_call_id = getattr(tool_call, 'id', 'unknown')
            else:
                function_name = tool_call.get('function', {}).get('name')
                arguments = tool_call.get('function', {}).get('arguments', '{}')
                tool_call_id = tool_call.get('id', 'unknown')
                
            if not function_name:
                return {
                    'tool_call_id': tool_call_id,
                    'error': "No function name provided",
                    'success': False
                }
                        
            if function_name not in self.tools:
                return {
                    'tool_call_id': tool_call_id,
                    'error': "Tool not found or not enabled",
                    'success': False
                }

            # A tool is callable if it's persistently enabled OR was
            # auto-enabled for this turn by passive_search() — both sets
            # were unioned into the tool list the model actually saw via
            # get_tool_definitions(), so both must be accepted here.
            if function_name not in self.enabled and function_name not in self.auto_enabled:
                return {
                    'tool_call_id': tool_call_id,
                    'error': "Tool not found or not enabled",
                    'success': False
                }

            # Permission guard — enforce even if LLM hallucinated a filtered tool.
            min_perm = self.tools[function_name].get('min_permission', 25)
            if caller_level < min_perm:
                return {
                    'tool_call_id': tool_call_id,
                    'error': (
                        f"[PERMISSION DENIED] '{caller_username}' has permission level "
                        f"{caller_level} but '{function_name}' requires {min_perm}."
                    ),
                    'success': False,
                }
                        
            if isinstance(arguments, str):
                try:
                    args = json.loads(arguments)
                except json.JSONDecodeError:
                    return {
                        'tool_call_id': tool_call_id,
                        'error': f"Invalid JSON in arguments: {arguments}",
                        'success': False
                    }
            else:
                args = arguments
                        
            # Coerce args to their annotated types where possible.
            # This handles LLMs serializing integers as strings, etc.
            args = self._coerce_args(function_name, args)

            # Call the function.
            # Async functions are awaited directly.
            # Sync functions are dispatched to a thread-pool executor so they
            # cannot block the event loop (and starve Discord heartbeats, etc.).
            fn = self.tools[function_name]['function']
            if inspect.iscoroutinefunction(fn):
                result = await fn(**args)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, functools.partial(fn, **args)
                )
                        
            return {
                'tool_call_id': tool_call_id,
                'function_name': function_name,
                'result': result,
                'success': True
            }
                    
        except Exception as e:
            return {
                'tool_call_id': getattr(tool_call, 'id', 'unknown'),
                'function_name': function_name if 'function_name' in locals() else 'unknown',
                'error': str(e),
                'success': False
            }
