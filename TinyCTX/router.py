"""
router.py — STUB (Phase 5 of runtime refactor).

Router, Lane, GroupLane, and _LaneRouter have been replaced by Runtime in
runtime.py. AgentLoop has been replaced by AgentCycle in cycle.py.

This stub raises RuntimeError on import so any surviving call sites fail
loudly rather than silently running stale code.

Will be deleted in Phase 8 once all references are removed.
"""

raise RuntimeError(
    "router.py has been replaced by runtime.py (Runtime). "
    "Remove any remaining imports of TinyCTX.router and update call sites to use "
    "TinyCTX.runtime.Runtime instead."
)
