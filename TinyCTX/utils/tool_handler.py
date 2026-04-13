import json
import inspect
import math
import re
from typing import Dict, Any, Callable, List, Optional

class ToolCallHandler:
    def __init__(self):
        self.tools: Dict[str, Any] = {}
        self.enabled: set[str] = set()

    def register_tool(self,
                      func: Callable,
                      name: Optional[str] = None,
                      description: Optional[str] = None,
                      always_on: bool = False):
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
            'required': required
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
        description_lines = []
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
        # Handle generic aliases like list[str], list[int], etc.
        origin = getattr(annotation, '__origin__', None)
        if origin is list:
            args = getattr(annotation, '__args__', None)
            item_schema = self._python_type_to_json_schema(args[0]) if args else {"type": "string"}
            return {"type": "array", "items": item_schema}

        mapping = {
            str:   {"type": "string"},
            int:   {"type": "integer"},
            float: {"type": "number"},
            bool:  {"type": "boolean"},
            dict:  {"type": "object"},
            list:  {"type": "array"},
        }
        return mapping.get(annotation, {"type": "string"})

    def enable(self, name: str) -> bool:
        """Enable a registered tool by name. Returns True if found, False if unknown."""
        if name not in self.tools:
            return False
        self.enabled.add(name)
        return True

    def tools_search(self, query: str) -> str:
        """Search for available tools by keyword and enable them for use.

        Args:
            query: Keyword or description of the capability you're looking for.
        """
        from TinyCTX.utils.bm25 import BM25

        corpus = {
            name: f"{name.replace('_', ' ')} {tool['description']}"
            for name, tool in self.tools.items()
        }
        if not corpus:
            return "No tools found matching that query."

        bm25   = BM25(corpus)
        scored = bm25.search(query, top_k=len(corpus))
        # Only surface results with a positive BM25 score
        hits   = [name for name, score in scored if score > 0.0]

        new     = [n for n in hits if n not in self.enabled]
        already = [n for n in hits if n in self.enabled]

        self.enabled.update(new)

        if not new:
            if already:
                return f"No new tools found. Already enabled: {', '.join(already)}"
            return "No tools found matching that query."
        return f"Enabled: {', '.join(new)}"

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        definitions = []
        for name in self.enabled:
            tool = self.tools.get(name)
            if tool is None:
                continue
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
    
    async def execute_tool_call(self, tool_call) -> Dict[str, Any]:
        """Execute a tool call from the LLM"""
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
                    'error': f"Tool '{function_name}' not found",
                    'available_tools': list(self.tools.keys()),
                    'success': False
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
                        
            # Call the function — await it if it's async
            result = self.tools[function_name]['function'](**args)
            if inspect.iscoroutine(result):
                result = await result
                        
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
