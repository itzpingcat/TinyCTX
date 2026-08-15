# tool_handling/__init__.py
#
# Tool registration, permission-gated execution, and discovery (BM25 today;
# vector_store.py / search.py land here as the vector + passive-search work
# builds out). Moved out of utils/ because ToolCallHandler carries real
# per-cycle state (self.tools, self.enabled) owned directly by AgentCycle —
# closer kin to agent.py/context.py than to utils/'s stateless helpers.

from TinyCTX.tool_handling.handler import ToolCallHandler

__all__ = [
    "ToolCallHandler",
]
