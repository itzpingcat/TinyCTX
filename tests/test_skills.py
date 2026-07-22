"""
tests/test_skills.py

Tests for modules/skills/__main__.py — frontmatter parsing, discovery
(skills + nested categories), the skill index prompt, category expansion
text, the use_skill / collapse_skill_categories tools, and the "skill fell
out of context" tagging + reminder mechanism (which relies on context.py's
HistoryEntry.tags / AssembleMeta.invalidated_tags — see test_context.py).

Uses a real ConversationDB(":memory:") and a real Context, plus a minimal
fake AgentCycle (config/tool_handler only — everything else is the real
thing) so register_agent() runs exactly as it would in production.

Run with:
    pytest tests/
"""
from __future__ import annotations

import json
import types

import pytest

from TinyCTX.db import ConversationDB
from TinyCTX.context import Context, ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL
from TinyCTX.contracts import ToolCall, ToolResult
from TinyCTX.modules.ctx_tools import __main__ as ctx_tools_mod
from TinyCTX.modules.skills import __main__ as skills_mod
from TinyCTX.modules.skills.__main__ import (
    _parse_frontmatter,
    _skill_body,
    _discover,
    _build_index_prompt,
    _expand_category_text,
    SkillEntry,
    CategoryNode,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class _ToolHandler:
    """Captures registered tools by name so tests can call them directly."""
    def __init__(self):
        self.tools = {}

    def register_tool(self, fn, **kwargs):
        self.tools[fn.__name__] = fn


class _Agent:
    """Minimal stand-in for the AgentCycle passed to register_agent — real
    ConversationDB and real Context underneath, only config/tool_handler faked."""
    def __init__(self, db, context, workspace, extra=None):
        self.db = db
        self.context = context
        self.config = types.SimpleNamespace(
            workspace=types.SimpleNamespace(path=str(workspace)),
            extra=extra or {},
        )
        self.tool_handler = _ToolHandler()


@pytest.fixture
def db():
    d = ConversationDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def isolate_home(tmp_path, monkeypatch):
    """Skills always scans ~/.agents/skills, cwd/.agents/skills, and
    ~/.tinyctx/skills in addition to the configured dir — isolate these to
    an empty tmp dir so real host directories can't leak into test results."""
    import pathlib
    fake_home = tmp_path / "_fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.chdir(tmp_path)
    return fake_home


def make_agent(db, workspace, extra=None):
    root = db.get_root()
    ctx = Context(db, tail_node_id=root.id, token_limit=100_000)
    return _Agent(db, ctx, workspace, extra=extra)


def write_skill(dir_path, name=None, description="", body="Do the thing."):
    dir_path.mkdir(parents=True, exist_ok=True)
    fm = "---\n"
    if name is not None:
        fm += f"name: {name}\n"
    fm += f"description: {description}\n---\n"
    (dir_path / "SKILL.md").write_text(fm + body, encoding="utf-8")


def write_category(dir_path, description=""):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "DESCRIPTION.md").write_text(f"---\ndescription: {description}\n---\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

class TestFrontmatter:
    def test_parses_simple_keys(self):
        text = "---\nname: foo\ndescription: does a thing\n---\nbody here"
        fm = _parse_frontmatter(text)
        assert fm == {"name": "foo", "description": "does a thing"}

    def test_no_frontmatter_returns_empty(self):
        assert _parse_frontmatter("just plain text, no frontmatter") == {}

    def test_strips_quotes(self):
        text = '---\nname: "quoted name"\n---\nbody'
        fm = _parse_frontmatter(text)
        assert fm["name"] == "quoted name"

    def test_lines_without_colon_ignored(self):
        text = "---\nname: foo\njust some line\n---\nbody"
        fm = _parse_frontmatter(text)
        assert fm == {"name": "foo"}

    def test_skill_body_strips_frontmatter(self):
        text = "---\nname: foo\n---\nThe actual body.\n"
        assert _skill_body(text) == "The actual body."

    def test_skill_body_no_frontmatter_returns_whole_text(self):
        assert _skill_body("just body text") == "just body text"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discovers_simple_skill(self, tmp_path):
        write_skill(tmp_path / "my_skill", name="my_skill", description="does stuff")
        skills, categories, top_level = _discover([tmp_path])
        assert "my_skill" in skills
        assert skills["my_skill"].description == "does stuff"
        assert len(top_level) == 1

    def test_skill_name_defaults_to_folder_name(self, tmp_path):
        write_skill(tmp_path / "folder_name", name=None, description="x")
        skills, _, _ = _discover([tmp_path])
        assert "folder_name" in skills

    def test_nested_category_with_skill(self, tmp_path):
        write_category(tmp_path / "cat1", description="a category")
        write_skill(tmp_path / "cat1" / "sub_skill", name="sub_skill", description="nested")
        skills, categories, top_level = _discover([tmp_path])
        assert "cat1" in categories
        assert "sub_skill" in skills
        assert skills["sub_skill"].category_path == "cat1"
        assert len(categories["cat1"].skills) == 1
        assert categories["cat1"].skills[0].name == "sub_skill"

    def test_arbitrarily_nested_categories(self, tmp_path):
        write_category(tmp_path / "a", description="a")
        write_category(tmp_path / "a" / "b", description="b")
        write_skill(tmp_path / "a" / "b" / "deep_skill", name="deep_skill", description="deep")
        skills, categories, _ = _discover([tmp_path])
        assert "a" in categories
        assert "a/b" in categories
        assert "deep_skill" in skills
        assert skills["deep_skill"].category_path == "a/b"
        assert categories["a"].subcategories[0].path == "a/b"

    def test_duplicate_skill_name_first_found_wins(self, tmp_path):
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        write_skill(dir_a / "dup", name="dup", description="from A")
        write_skill(dir_b / "dup", name="dup", description="from B")
        skills, _, _ = _discover([dir_a, dir_b])
        assert skills["dup"].description == "from A"

    def test_folder_with_both_files_treated_as_skill(self, tmp_path):
        d = tmp_path / "both"
        write_skill(d, name="both", description="skill wins")
        write_category(d, description="ignored")
        skills, categories, _ = _discover([tmp_path])
        assert "both" in skills
        assert "both" not in categories

    def test_plain_folder_ignored(self, tmp_path):
        (tmp_path / "nothing_here").mkdir()
        skills, categories, top_level = _discover([tmp_path])
        assert skills == {}
        assert categories == {}
        assert top_level == []

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        skills, categories, top_level = _discover([tmp_path / "does_not_exist"])
        assert skills == {} and categories == {} and top_level == []


# ---------------------------------------------------------------------------
# Index prompt building
# ---------------------------------------------------------------------------

class TestIndexPromptBuilding:
    def test_empty_top_level_returns_none(self):
        assert _build_index_prompt([], set(), {}) is None

    def test_skill_rendered_in_full(self):
        skill = SkillEntry(name="foo", description="does foo", skill_md="path/to/SKILL.md", category_path="")
        prompt = _build_index_prompt([skill], set(), {})
        assert "foo" in prompt
        assert "does foo" in prompt
        assert "path/to/SKILL.md" in prompt

    def test_category_collapsed_by_default(self):
        cat = CategoryNode(name="cat1", path="cat1", description="a category")
        prompt = _build_index_prompt([cat], set(), {"cat1": cat})
        assert "cat1" in prompt
        assert "skill_category_hint" in prompt

    def test_category_expanded_when_in_expanded_set(self):
        cat = CategoryNode(name="cat1", path="cat1", description="a category")
        inner = SkillEntry(name="inner", description="inner skill", skill_md="x", category_path="cat1")
        cat.skills.append(inner)
        prompt = _build_index_prompt([cat], {"cat1"}, {"cat1": cat})
        assert "inner" in prompt
        assert "inner skill" in prompt

    def test_no_hint_when_nothing_collapsed(self):
        skill = SkillEntry(name="foo", description="x", skill_md="x", category_path="")
        prompt = _build_index_prompt([skill], set(), {})
        assert "skill_category_hint" not in prompt


# ---------------------------------------------------------------------------
# Category expansion text (tool output for use_skill(category_path))
# ---------------------------------------------------------------------------

class TestCategoryExpansionText:
    def test_lists_skills_and_subcategories(self):
        cat = CategoryNode(name="cat1", path="cat1", description="top desc")
        cat.skills.append(SkillEntry(name="s1", description="s1 desc", skill_md="p1", category_path="cat1"))
        sub = CategoryNode(name="sub", path="cat1/sub", description="sub desc")
        cat.subcategories.append(sub)
        text = _expand_category_text(cat)
        assert "cat1" in text
        assert "s1" in text and "s1 desc" in text
        assert "sub" in text and 'use_skill("cat1/sub")' in text


# ---------------------------------------------------------------------------
# use_skill tool (via register_agent, real Context/DB)
# ---------------------------------------------------------------------------

class TestUseSkillTool:
    def test_loads_skill_body(self, db, tmp_path, isolate_home):
        write_skill(tmp_path / "skills" / "foo", name="foo", description="d", body="Foo instructions.")
        agent = make_agent(db, tmp_path)
        skills_mod.register_agent(agent)
        result = agent.tool_handler.tools["use_skill"]("foo")
        assert "Foo instructions." in result
        assert result.startswith("# Skill: foo")

    def test_expands_category(self, db, tmp_path, isolate_home):
        write_category(tmp_path / "skills" / "cat1", description="a category")
        write_skill(tmp_path / "skills" / "cat1" / "inner", name="inner", description="d")
        agent = make_agent(db, tmp_path)
        skills_mod.register_agent(agent)
        result = agent.tool_handler.tools["use_skill"]("cat1")
        assert "inner" in result

    def test_case_insensitive_match(self, db, tmp_path, isolate_home):
        write_skill(tmp_path / "skills" / "Foo", name="Foo", description="d", body="body")
        agent = make_agent(db, tmp_path)
        skills_mod.register_agent(agent)
        result = agent.tool_handler.tools["use_skill"]("foo")
        assert "body" in result

    def test_not_found_returns_error_listing(self, db, tmp_path, isolate_home):
        write_skill(tmp_path / "skills" / "known", name="known", description="d")
        agent = make_agent(db, tmp_path)
        skills_mod.register_agent(agent)
        result = agent.tool_handler.tools["use_skill"]("nonexistent")
        assert "not found" in result
        assert "known" in result

    def test_skill_index_injected_into_system_prompt(self, db, tmp_path, isolate_home):
        write_skill(tmp_path / "skills" / "foo", name="foo", description="does foo")
        agent = make_agent(db, tmp_path)
        skills_mod.register_agent(agent)
        messages, _ = agent.context.assemble()
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "foo" in system_msg["content"]
        assert "does foo" in system_msg["content"]


# ---------------------------------------------------------------------------
# collapse_skill_categories tool + ephemeral vs persistent expansion
# ---------------------------------------------------------------------------

class TestCategoryExpansionPersistence:
    def test_ephemeral_default_does_not_persist_expansion(self, db, tmp_path, isolate_home):
        write_category(tmp_path / "skills" / "cat1", description="d")
        write_skill(tmp_path / "skills" / "cat1" / "inner", name="inner", description="d")
        agent = make_agent(db, tmp_path)
        skills_mod.register_agent(agent)

        agent.tool_handler.tools["use_skill"]("cat1")  # expand once
        messages, _ = agent.context.assemble()
        system_msg = next(m for m in messages if m["role"] == "system")
        # ephemeral=True (default) — category index stays collapsed next turn
        assert "skill_category_hint" in system_msg["content"]

    def test_persistent_mode_keeps_category_expanded(self, db, tmp_path, isolate_home):
        write_category(tmp_path / "skills" / "cat1", description="d")
        write_skill(tmp_path / "skills" / "cat1" / "inner", name="inner", description="d")
        agent = make_agent(db, tmp_path, extra={"skills": {"ephemeral_categories": False}})
        skills_mod.register_agent(agent)

        agent.tool_handler.tools["use_skill"]("cat1")
        messages, _ = agent.context.assemble()
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "inner" in system_msg["content"]  # expanded inline now

    def test_collapse_skill_categories_removes_expansion(self, db, tmp_path, isolate_home):
        write_category(tmp_path / "skills" / "cat1", description="d")
        write_skill(tmp_path / "skills" / "cat1" / "inner", name="inner", description="d")
        agent = make_agent(db, tmp_path, extra={"skills": {"ephemeral_categories": False}})
        skills_mod.register_agent(agent)

        agent.tool_handler.tools["use_skill"]("cat1")
        result = agent.tool_handler.tools["collapse_skill_categories"](["cat1"])
        assert "Collapsed: cat1" in result

        messages, _ = agent.context.assemble()
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "skill_category_hint" in system_msg["content"]  # back to collapsed

    def test_collapse_no_op_when_ephemeral(self, db, tmp_path, isolate_home):
        write_category(tmp_path / "skills" / "cat1", description="d")
        agent = make_agent(db, tmp_path)  # ephemeral=True (default)
        skills_mod.register_agent(agent)
        result = agent.tool_handler.tools["collapse_skill_categories"](["cat1"])
        assert "ephemeral" in result.lower()

    def test_collapse_star_collapses_all(self, db, tmp_path, isolate_home):
        write_category(tmp_path / "skills" / "cat1", description="d")
        write_category(tmp_path / "skills" / "cat2", description="d")
        agent = make_agent(db, tmp_path, extra={"skills": {"ephemeral_categories": False}})
        skills_mod.register_agent(agent)
        agent.tool_handler.tools["use_skill"]("cat1")
        agent.tool_handler.tools["use_skill"]("cat2")
        result = agent.tool_handler.tools["collapse_skill_categories"](["*"])
        assert "cat1" in result and "cat2" in result


# ---------------------------------------------------------------------------
# "Skill fell out of context" tagging + reminder (full integration, with
# ctx_tools' real trim wired in, exactly as it runs in production)
# ---------------------------------------------------------------------------

class TestSkillDroppedReminder:
    def _add_turn(self, db, ctx, i):
        """One real turn: user msg + assistant reply, tail advanced, assemble() called."""
        from TinyCTX.context import HistoryEntry
        ctx.add(HistoryEntry.user(f"filler {i} " * 5, author_id="kamie"))
        messages, meta = ctx.assemble()
        ctx.add(HistoryEntry.assistant(f"reply {i}"))
        return messages, meta

    def _has_reminder(self, messages):
        return any(m["role"] == "user" and "skill_reminder" in m.get("content", "") for m in messages)

    def test_fresh_skill_load_not_flagged(self, db, tmp_path, isolate_home):
        write_skill(tmp_path / "skills" / "foo", name="foo", description="d", body="body")
        agent = make_agent(db, tmp_path)
        ctx_tools_mod.register_agent(agent)
        skills_mod.register_agent(agent)

        tc = ToolCall.make("use_skill", {"name": "foo"})
        ctx = agent.context
        _mk_assistant(ctx, tc)
        ctx.add_tool_result(ToolResult(call_id=tc.call_id, tool_name="use_skill", output="# Skill: foo\n\nbody"))

        messages, meta = ctx.assemble()
        assert not self._has_reminder(messages)

    def test_reminder_fires_once_then_stops(self, db, tmp_path, isolate_home):
        write_skill(tmp_path / "skills" / "foo", name="foo", description="d", body="body")
        agent = make_agent(db, tmp_path)
        ctx_tools_mod.register_agent(agent)
        skills_mod.register_agent(agent)
        ctx = agent.context

        tc = ToolCall.make("use_skill", {"name": "foo"})
        _mk_assistant(ctx, tc)
        ctx.add_tool_result(ToolResult(call_id=tc.call_id, tool_name="use_skill", output="# Skill: foo\n\nbody"))

        # Fresh load — not yet invalidated.
        messages0, meta0 = ctx.assemble()
        assert not self._has_reminder(messages0)

        # ctx_tools' default tool_trim_after is 25 — advance turns until the
        # skill result ages past that and gets stubbed by the real trim hook.
        reminder_turn = None
        for i in range(1, 40):
            messages, meta = self._add_turn(db, ctx, i)
            if self._has_reminder(messages):
                reminder_turn = i
                break

        assert reminder_turn is not None, "expected the reminder to appear once the skill aged out"

        # One more turn — must not refire.
        messages_next, _ = self._add_turn(db, ctx, reminder_turn + 1000)
        assert not self._has_reminder(messages_next)


def _mk_assistant(ctx, tool_call):
    from TinyCTX.context import HistoryEntry
    return ctx.add(HistoryEntry.assistant("", tool_calls=[tool_call]))
