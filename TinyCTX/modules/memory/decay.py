"""
modules/memory/decay.py — REMOVED in v2.

The automatic decay sweep that hard-deleted entities is gone. It destroyed
quiet-but-important data by normalising factors relative to each sweep's
population and DETACH DELETE-ing anything below a threshold with no review.

Its replacement is the `decay_candidate` flagger (flaggers/decay_candidate.py),
which flags stale/quiet/isolated entities for the Reviewer librarian to assess.
Nothing is deleted without a judgment step.

This stub remains only because file deletion is unavailable in this environment.
Do not import it.
"""
