<equipment_manifest>

# Equipment Manifest (EM)

- **Date:** {{ date }}
- **OS:** {{ system }}
- **Workspace:** {{ workspace_path }}
{%- if config_path %}
- **Config:** {{ config_path }}
{%- endif %}
- **Source root:** {{ source_root }}
{%- if source_root != workspace_path %}
- The source root above is where TinyCTX's own code lives. When the user asks about the code, the repo, or "your code", start from that path — do not rediscover it with shell listings.
{%- endif %}

{% if system == 'Windows' %}
## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or PowerShell when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled (`[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`).
- Prefer view(), grep(), and glob_search() for file inspection when they are sufficient.
{% else %}
## Platform Policy (POSIX)
- You are running on a POSIX system ({{ system }}). Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
{% endif %}
</equipment_manifest>