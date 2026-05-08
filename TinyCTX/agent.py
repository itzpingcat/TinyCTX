"""
agent.py — STUB (Phase 4 of runtime refactor).

AgentLoop and run_background() have been replaced by AgentCycle and
CycleHooks in cycle.py. Runtime (runtime.py, Phase 5) constructs
AgentCycle directly.

This stub raises RuntimeError on import so any surviving call sites
fail loudly rather than silently running stale code.

Will be deleted in Phase 8 once all references are removed.
"""

raise RuntimeError(
    "agent.py has been replaced by cycle.py (AgentCycle) and runtime.py (Runtime). "
    "Remove any remaining imports of TinyCTX.agent and update call sites to use "
    "TinyCTX.cycle.AgentCycle / TinyCTX.runtime.Runtime instead."
)
