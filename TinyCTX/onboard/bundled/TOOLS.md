# TOOLS.md

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## Tool Sources

**Always-on** (in your tool list from turn one):

- `tools_search` — use this to find and enable additional tools by keyword
- `shell`, `view`, `write_file`, `str_replace`, `grep`, `glob_search` — filesystem operations, sandboxed to workspace
- `web_search`, `open_url` — web access
- `memory_search` — semantic search over your memory files
- `use_skill` — load a skill's full instructions on demand
- `cron_list` — view scheduled jobs

**Deferred** (registered but hidden until you search for them):

- Browser interaction tools: `click`, `type_text`, `extract_text`, `extract_html`, `screenshot`, `wait_for`, `manage_browser`
- `http_request` — raw HTTP calls

To enable a deferred tool, call `tools_search` with a relevant keyword (e.g. `tools_search("screenshot")` or `tools_search("http request")`). Tools with a positive relevance score are enabled immediately and stay enabled for the rest of the session. You only need to search once per session — enabled tools persist across turns and survive `/reset`.

### Skills (agentskills.io)

`use_skill` is always available. Call it with the skill name shown in the `<available_skills>` index in your system prompt. Read the skill's instructions before using it — don't guess at behavior.

## Tool Usage Notes

**shell/bash** — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

**glob** — File Discovery

- Use `glob` to find files by pattern before falling back to shell commands
- Simple patterns like `*.py` match recursively by filename
- Use `entry_type=\"dirs\"` when you need matching directories instead of files
- Use `head_limit` and `offset` to page through large result sets
- Prefer this over commands when you only need file paths

**grep** — Content Search

- Use `grep` to search file contents inside the workspace
- Default behavior returns only matching file paths (`output_mode=\"files_with_matches\"`)
- Supports optional `glob` filtering plus `context_before` / `context_after`
- Supports `type=\"py\"`, `type=\"ts\"`, `type=\"md\"` and similar shorthand filters
- Use `fixed_strings=true` for literal keywords containing regex characters
- Use `output_mode=\"files_with_matches\"` to get only matching file paths
- Use `output_mode=\"count\"` to size a search before reading full matches
- Use `head_limit` and `offset` to page across results
- Prefer this over commands for code and history searches
- Binary or oversized files may be skipped to keep results readable

**cron** — Scheduled Reminders

- Please refer to cron skill for usage.