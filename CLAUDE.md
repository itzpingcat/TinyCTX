# CLAUDE.md (aka AGENTS.md)

This is the LLM entrypoint for the TinyCTX project. It lists behavioral rules for agents contributing to the project. If you are looking for the overview of the codebase, it is in `CODEBASE.md`.

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Conventions

- Flat structure (≤2 directory levels).
- Functions over classes unless persistent shared state required.
- Direct library usage; avoid abstraction layers (no LangChain, no Celery).
- Simple data types over complex structures.
- Configuration via config.yaml.
- NO MAGIC VALUES that aren't configurable.
- Ensure logging to catch errors.
- One module = one job = one sentence description.
- Files under 600 lines
- Ensure to run linters post changes
- Tests to be stored in /tests
- Scripts to be stored in /scripts
- Docs to be stored in /docs

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Other Tool Specifics
- Use edit_file instead of write_file if you are changing less than 50% of a file.
- Use read_multiple_files when reading multiple files.
- Use powershell to grep for uses of functions, variables, etc.
- If you do not see these tools, try using tools_search or a utility that surfaces them.

## CODEBASE.md
- If this file is missing, explore the codebase and autogenerate it.
    - when you autogenerate it: Write a TODO: list of functions not yet explored (get a directory tree)
    - Then start reading general files, before specific files.
    - Every ~8 files you read, write to the CODEBASE.md file, and mark off items in the TODO as done.
- Always update CODEBASE.md when you make changes to the code.
- Read CODEBASE.md to find where things are, it's faster than manually searching.
