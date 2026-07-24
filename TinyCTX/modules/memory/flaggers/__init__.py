"""
modules/memory/flaggers/

Dynamically-loaded graph-scanning snippets for the Reviewer librarian. Each
module exposes:

    FLAGGER_TYPE: str
    scan(graph_db, cfg) -> list[dict]      # issue dicts
    build_prompt(issue) -> str             # reviewer instruction for one issue

Issue dict: {flagger_type, entity_uuids: [...], scope: str, detail: str}.
Flaggers scan the whole graph (system-level) via graph_db.safe_execute; the
Reviewer then operates within the scope each issue declares.
"""
