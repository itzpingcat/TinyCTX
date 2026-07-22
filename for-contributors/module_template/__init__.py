"""
__init__.py — module metadata and default config.

TinyCTX modules that need configurable behavior declare an EXTENSION_META
dict here. It is NOT required for module discovery (module_registry.py only
looks for register_runtime/register_agent in __main__.py or __init__.py) —
it's a convention for self-describing, configurable defaults. See
modules/todo/__init__.py, modules/rag/__init__.py, modules/skills/__init__.py,
modules/cron/__init__.py for real examples of this exact shape.

"default_config" values are the module's own defaults. A deployer can
override any of them per-instance via config.yaml under:

    extra:
      <module_name>:
        <key>: <override_value>

__main__.py then merges the two (runtime config wins) — see the
"Config: default_config + config.yaml merge" section in __main__.py's
register_agent for the merge pattern.
"""

EXTENSION_META = {
    "name":    "example_module",
    "version": "1.0",
    # Optional: set to "singleton" for modules that run once on a system
    # lane rather than per-lane (see modules/cron/__init__.py). Omit this
    # key entirely for a normal per-cycle module.
    # "module_type": "singleton",
    "description": (
        "One or two sentences describing what this module does and which "
        "tools/prompts it adds. This shows up in tooling that lists modules "
        "— keep it accurate and current as the module evolves."
    ),
    "default_config": {
        # Every tunable your module reads should have a default here, with
        # a comment explaining what it controls. Deployers override these
        # via config.yaml's `extra.<module_name>.<key>` — never require a
        # config.yaml edit just to run with sane defaults.
        "prompt_priority": 8,
        # "some_other_setting": "default_value",
    },
}
