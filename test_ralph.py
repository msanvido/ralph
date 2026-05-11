"""Tests. Run with: python -m unittest test_ralph"""

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ralph import cli as ralph
from ralph import config
from ralph import memory
from ralph import tools as tools_mod
from ralph.bus import Bus

VALID_TOOL = """
TOOL = {
    "name": "shout",
    "description": "Shout the input.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}

def run(**kwargs):
    return {"shouted": kwargs["text"].upper()}
"""

DUP_TOOL = """
TOOL = {
    "name": "dup",
    "description": "x",
    "input_schema": {"type": "object", "properties": {}},
}

def run(**kw):
    return "ok"
"""

SHADOW_TOOL = """
TOOL = {
    "name": "ls",
    "description": "x",
    "input_schema": {"type": "object", "properties": {}},
}

def run(**kw):
    return {}
"""


# === Helpers for building OpenAI-shaped mock responses ===

def fake_tool_call(call_id: str, name: str, args: dict):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def fake_response(content=None, tool_calls=None, finish_reason="stop"):
    msg = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)])


# === Tests ===

class LoadDynamicToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tools_dir = Path(self._tmp.name)
        self._patch = patch.object(config, "TOOLS_DIR", self.tools_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, name: str, body: str) -> None:
        (self.tools_dir / name).write_text(body)

    def test_loads_valid_tool(self):
        self._write("shout.py", VALID_TOOL)
        schemas, dispatch, _ = tools_mod.load_dynamic_tools()
        self.assertEqual([s.get("name") for s in schemas], ["shout"])
        self.assertEqual(dispatch["shout"](text="hi"), {"shouted": "HI"})

    def test_skips_underscore_files(self):
        self._write("_helper.py", VALID_TOOL)
        schemas, dispatch, _ = tools_mod.load_dynamic_tools()
        self.assertEqual(schemas, [])
        self.assertEqual(dispatch, {})

    def test_skips_files_missing_metadata(self):
        self._write("broken.py", "x = 1\n")
        schemas, _, _ = tools_mod.load_dynamic_tools()
        self.assertEqual(schemas, [])

    def test_skips_files_with_load_errors(self):
        self._write("boom.py", "raise RuntimeError('nope')\n")
        schemas, _, warnings = tools_mod.load_dynamic_tools()
        self.assertEqual(schemas, [])
        self.assertTrue(any("boom.py" in w for w in warnings))

    def test_skips_builtin_shadow(self):
        self._write("shadow.py", SHADOW_TOOL)
        schemas, dispatch, _ = tools_mod.load_dynamic_tools()
        self.assertEqual(schemas, [])
        self.assertNotIn("ls", dispatch)

    def test_skips_duplicate_names(self):
        self._write("a.py", DUP_TOOL)
        self._write("b.py", DUP_TOOL)
        schemas, dispatch, _ = tools_mod.load_dynamic_tools()
        self.assertEqual([s.get("name") for s in schemas], ["dup"])
        self.assertEqual(len(dispatch), 1)


class ToOpenAIToolTests(unittest.TestCase):
    def test_anthropic_format_normalized(self):
        out = tools_mod.to_openai_tool({
            "name": "ls",
            "description": "list files",
            "input_schema": {"type": "object", "properties": {}},
        })
        self.assertEqual(out["type"], "function")
        self.assertEqual(out["function"]["name"], "ls")
        self.assertEqual(out["function"]["description"], "list files")
        self.assertEqual(out["function"]["parameters"], {"type": "object", "properties": {}})

    def test_openai_format_passthrough(self):
        already = {
            "type": "function",
            "function": {"name": "x", "description": "y", "parameters": {"type": "object"}},
        }
        self.assertIs(tools_mod.to_openai_tool(already), already)

    def test_missing_input_schema_defaults(self):
        out = tools_mod.to_openai_tool({"name": "noargs", "description": ""})
        self.assertEqual(out["function"]["parameters"], {"type": "object", "properties": {}})


class BuiltinToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self._patches = [
            patch.object(config, "WORKSPACE", self.ws),
            patch.object(config, "DONE_MARKER", self.ws / "DONE"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_write_then_read_roundtrip(self):
        self.assertEqual(
            json.loads(tools_mod.handle_tool("write", {"path": "a.txt", "content": "hi"}, {})),
            {"status": "written", "bytes": 2},
        )
        self.assertEqual(
            json.loads(tools_mod.handle_tool("read", {"path": "a.txt"}, {})),
            {"content": "hi"},
        )

    def test_write_creates_parent_dirs(self):
        tools_mod.handle_tool("write", {"path": "deep/nested/file.txt", "content": "x"}, {})
        self.assertTrue((self.ws / "deep" / "nested" / "file.txt").exists())

    def test_edit_replaces_unique_string(self):
        (self.ws / "f.txt").write_text("hello world")
        result = json.loads(tools_mod.handle_tool(
            "edit", {"path": "f.txt", "old_string": "world", "new_string": "ralph"}, {},
        ))
        self.assertEqual(result, {"status": "edited", "replacements": 1})
        self.assertEqual((self.ws / "f.txt").read_text(), "hello ralph")

    def test_edit_errors_on_ambiguous_match(self):
        (self.ws / "f.txt").write_text("foo foo foo")
        result = json.loads(tools_mod.handle_tool(
            "edit", {"path": "f.txt", "old_string": "foo", "new_string": "bar"}, {},
        ))
        self.assertIn("error", result)
        self.assertIn("3 times", result["error"])

    def test_edit_replace_all(self):
        (self.ws / "f.txt").write_text("foo foo foo")
        result = json.loads(tools_mod.handle_tool(
            "edit",
            {"path": "f.txt", "old_string": "foo", "new_string": "bar", "replace_all": True},
            {},
        ))
        self.assertEqual(result, {"status": "edited", "replacements": 3})
        self.assertEqual((self.ws / "f.txt").read_text(), "bar bar bar")

    def test_edit_errors_when_string_missing(self):
        (self.ws / "f.txt").write_text("hello")
        result = json.loads(tools_mod.handle_tool(
            "edit", {"path": "f.txt", "old_string": "nope", "new_string": "x"}, {},
        ))
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])

    def test_grep_returns_matches_with_line_numbers(self):
        (self.ws / "a.py").write_text("import os\nprint('hi')\n")
        (self.ws / "b.txt").write_text("nothing here\n")
        result = json.loads(tools_mod.handle_tool("grep", {"pattern": r"^import"}, {}))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["matches"][0]["file"], "a.py")
        self.assertEqual(result["matches"][0]["line"], 1)

    def test_grep_with_glob_filter(self):
        (self.ws / "a.py").write_text("token\n")
        (self.ws / "a.txt").write_text("token\n")
        result = json.loads(tools_mod.handle_tool("grep", {"pattern": "token", "glob": "*.py"}, {}))
        self.assertEqual([m["file"] for m in result["matches"]], ["a.py"])

    def test_find_matches_glob(self):
        (self.ws / "x.py").write_text("")
        (self.ws / "y.txt").write_text("")
        (self.ws / "sub").mkdir()
        (self.ws / "sub" / "z.py").write_text("")
        result = json.loads(tools_mod.handle_tool("find", {"name": "*.py"}, {}))
        self.assertEqual(set(result["paths"]), {"x.py", "sub/z.py"})

    def test_ls_default_one_level(self):
        (self.ws / "a.txt").write_text("")
        (self.ws / "sub").mkdir()
        (self.ws / "sub" / "b.txt").write_text("")
        result = json.loads(tools_mod.handle_tool("ls", {}, {}))
        self.assertIn("a.txt", result["paths"])
        self.assertIn("sub/", result["paths"])
        self.assertNotIn("sub/b.txt", result["paths"])

    def test_ls_recursive(self):
        (self.ws / "a.txt").write_text("")
        (self.ws / "sub").mkdir()
        (self.ws / "sub" / "b.txt").write_text("")
        result = json.loads(tools_mod.handle_tool("ls", {"recursive": True}, {}))
        self.assertEqual(set(result["paths"]), {"a.txt", "sub/b.txt"})

    def test_path_traversal_blocked(self):
        result = json.loads(tools_mod.handle_tool("read", {"path": "../escape.txt"}, {}))
        self.assertIn("error", result)
        self.assertIn("escapes workspace", result["error"])

    def test_mark_done_writes_marker(self):
        tools_mod.handle_tool("mark_done", {}, {})
        self.assertTrue((self.ws / "DONE").exists())


class HandleToolDispatchTests(unittest.TestCase):
    def test_string_return_passthrough(self):
        dispatch = {"hello": lambda **kw: "world"}
        self.assertEqual(tools_mod.handle_tool("hello", {}, dispatch), "world")

    def test_dict_return_json_encoded(self):
        dispatch = {"hello": lambda **kw: {"a": 1}}
        self.assertEqual(json.loads(tools_mod.handle_tool("hello", {}, dispatch)), {"a": 1})

    def test_kwargs_are_forwarded(self):
        dispatch = {"echo": lambda **kw: kw}
        result = json.loads(tools_mod.handle_tool("echo", {"x": 1, "y": "z"}, dispatch))
        self.assertEqual(result, {"x": 1, "y": "z"})

    def test_dynamic_exceptions_returned_as_error(self):
        def boom(**kw):
            raise RuntimeError("bad")
        result = json.loads(tools_mod.handle_tool("boom", {}, {"boom": boom}))
        self.assertIn("error", result)
        self.assertIn("RuntimeError", result["error"])

    def test_unknown_tool(self):
        result = json.loads(tools_mod.handle_tool("missing", {}, {}))
        self.assertIn("error", result)
        self.assertIn("unknown tool", result["error"])


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.mem = self.ws / "memory"
        self._patches = [
            patch.object(config, "WORKSPACE", self.ws),
            patch.object(config, "MEMORY_DIR", self.mem),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    @staticmethod
    def _l(description: str, prompt: str | None = None) -> dict:
        """Build a {description, prompt} lesson dict."""
        return {"description": description, "prompt": prompt or f"FULL: {description}"}

    def test_append_memory_writes_lesson_files_and_index(self):
        count = memory.append_memory({
            "recipes": [self._l("call ls before reading"), self._l("use edit for surgical changes")],
        })
        self.assertEqual(count, 2)
        recipes_dir = self.mem / "recipes"
        self.assertTrue((recipes_dir / "_index.py").exists())
        self.assertTrue((recipes_dir / "call_ls_before_reading.py").exists())
        self.assertTrue((recipes_dir / "use_edit_for_surgical_changes.py").exists())
        # No other category directories should be created.
        self.assertFalse((self.mem / "worked").exists())
        self.assertFalse((self.mem / "failed").exists())
        # Lesson file holds both fields.
        lesson = memory.load_lesson("recipes", "call_ls_before_reading")
        self.assertEqual(lesson["description"], "call ls before reading")
        self.assertEqual(lesson["prompt"], "FULL: call ls before reading")
        # Index lists the lesson.
        index = memory.load_index("recipes")
        self.assertEqual(
            sorted(e["id"] for e in index),
            ["call_ls_before_reading", "use_edit_for_surgical_changes"],
        )

    def test_append_memory_skips_empty_or_malformed_entries(self):
        count = memory.append_memory({
            "recipes": [
                {"description": "", "prompt": "x"},
                {"description": "x", "prompt": ""},
                "not-a-dict",
                None,
                {"description": "valid", "prompt": "valid prompt"},
            ],
        })
        self.assertEqual(count, 1)
        self.assertEqual([e["id"] for e in memory.load_index("recipes")], ["valid"])

    def test_append_memory_disambiguates_colliding_slugs(self):
        # Two descriptions slugify to the same id — second should get a _2 suffix.
        memory.append_memory({"recipes": [self._l("Use ls!")]})
        memory.append_memory({"recipes": [self._l("use ls?")]})
        ids = [e["id"] for e in memory.load_index("recipes")]
        self.assertEqual(ids, ["use_ls", "use_ls_2"])
        self.assertTrue((self.mem / "recipes" / "use_ls.py").exists())
        self.assertTrue((self.mem / "recipes" / "use_ls_2.py").exists())

    def test_append_memory_ignores_unknown_categories(self):
        # 'worked' and 'failed' are no longer categories — they get silently dropped.
        count = memory.append_memory({
            "worked": [self._l("dropped")],
            "failed": [self._l("dropped")],
            "recipes": [self._l("kept")],
        })
        self.assertEqual(count, 1)
        self.assertEqual([e["id"] for e in memory.load_index("recipes")], ["kept"])
        self.assertFalse((self.mem / "worked").exists())
        self.assertFalse((self.mem / "failed").exists())

    def test_format_transcript_handles_openai_messages(self):
        messages = [
            {"role": "user", "content": "do the thing"},
            {
                "role": "assistant",
                "content": "thinking...",
                "tool_calls": [
                    {"id": "tu_1", "type": "function",
                     "function": {"name": "ls", "arguments": json.dumps({"recursive": True})}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": '{"ok": true}'},
        ]
        out = memory.format_transcript(messages)
        self.assertIn("USER: do the thing", out)
        self.assertIn("ASSISTANT: thinking...", out)
        self.assertIn("TOOL_USE ls(", out)
        self.assertIn("TOOL_RESULT[tu_1]", out)

    def test_read_memory_items_returns_category_prefixed_ids(self):
        memory.append_memory({
            "recipes": [self._l("call ls first"), self._l("use edit")],
        })
        items = memory.read_memory_items()
        ids = [i[0] for i in items]
        self.assertIn("recipes:call_ls_first", ids)
        self.assertIn("recipes:use_edit", ids)
        # The third element is the (short) description — not the full prompt.
        descriptions = {i[0]: i[2] for i in items}
        self.assertEqual(descriptions["recipes:call_ls_first"], "call ls first")

    def test_select_returns_empty_when_no_memory(self):
        with patch.object(config, "completion") as mock:
            self.assertEqual(memory.select_relevant_memories("any task"), "")
            mock.assert_not_called()

    def test_select_loads_full_prompts_for_chosen_ids(self):
        memory.append_memory({
            "recipes": [
                {"description": "ls first", "prompt": "FULL ls prompt"},
                {"description": "use edit", "prompt": "FULL edit prompt"},
                {"description": "argparse flow", "prompt": "FULL argparse prompt"},
            ],
        })
        response = fake_response(
            tool_calls=[fake_tool_call("c", "select_memories", {
                "ids": ["recipes:ls_first", "recipes:argparse_flow"],
            })],
            finish_reason="tool_calls",
        )
        with patch.object(config, "completion", return_value=response):
            preamble = memory.select_relevant_memories("write a CLI flag")
        # Selected recipes appear via their full prompt (not just description).
        self.assertIn("FULL ls prompt", preamble)
        self.assertIn("FULL argparse prompt", preamble)
        self.assertNotIn("FULL edit prompt", preamble)
        self.assertIn("Recipes from past iterations", preamble)

    def test_select_catalog_uses_descriptions_not_prompts(self):
        memory.append_memory({
            "recipes": [{"description": "SHORT desc", "prompt": "LONG prompt body here"}],
        })
        response = fake_response(
            tool_calls=[fake_tool_call("c", "select_memories", {"ids": []})],
            finish_reason="tool_calls",
        )
        with patch.object(config, "completion", return_value=response) as mock:
            memory.select_relevant_memories("THE TASK")
        user_content = next(
            m["content"] for m in mock.call_args.kwargs["messages"] if m["role"] == "user"
        )
        self.assertIn("THE TASK", user_content)
        self.assertIn("recipes:short_desc", user_content)
        self.assertIn("SHORT desc", user_content)
        # The full prompt body should NOT be in the selection catalog.
        self.assertNotIn("LONG prompt body", user_content)

    def test_select_swallows_api_errors(self):
        memory.append_memory({"recipes": [self._l("x")]})
        with patch.object(config, "completion", side_effect=RuntimeError("boom")):
            self.assertEqual(memory.select_relevant_memories("task"), "")

    def test_learn_from_iteration_skips_empty(self):
        with patch.object(config, "completion") as mock:
            memory.learn_from_iteration([{"role": "user", "content": "hi"}], 1)
            mock.assert_not_called()

    def test_learn_from_iteration_writes_lesson_files(self):
        response = fake_response(
            tool_calls=[fake_tool_call("c", "record_learnings", {
                "recipes": [
                    {"description": "ls first", "prompt": "Always call ls before reading."},
                    {"description": "use edit", "prompt": "Use edit for surgical changes."},
                ],
            })],
            finish_reason="tool_calls",
        )
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "done"},
        ]
        with patch.object(config, "completion", return_value=response) as mock:
            memory.learn_from_iteration(msgs, 7)
            self.assertEqual(mock.call_count, 1)
            self.assertEqual(
                mock.call_args.kwargs["tool_choice"],
                {"type": "function", "function": {"name": "record_learnings"}},
            )
        ls_first = memory.load_lesson("recipes", "ls_first")
        self.assertEqual(ls_first["description"], "ls first")
        self.assertIn("Always call ls", ls_first["prompt"])
        use_edit = memory.load_lesson("recipes", "use_edit")
        self.assertEqual(use_edit["description"], "use edit")
        self.assertIn("Use edit for surgical", use_edit["prompt"])
        # Old worked/failed dirs are never created.
        self.assertFalse((self.mem / "worked").exists())
        self.assertFalse((self.mem / "failed").exists())


class ToolsLRUTests(unittest.TestCase):
    """Dynamic-tool LRU: capped count, oldest-first eviction, touch on call."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tools_dir = Path(self._tmp.name)
        self._patches = [
            patch.object(config, "TOOLS_DIR", self.tools_dir),
            patch.object(tools_mod, "TOOLS_LRU_LIMIT", 3),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _seed(self, name: str, ts: float | None = None) -> None:
        tool_src = (
            f'TOOL = {{"name": "{name}", "description": "x", '
            '"input_schema": {"type": "object", "properties": {}}}\n'
            'def run(**kw): return {"ok": True}\n'
        )
        (self.tools_dir / f"{name}.py").write_text(tool_src)
        if ts is not None:
            lru = tools_mod._TOOLS_LRU.load()
            lru[f"{name}.py"] = ts
            tools_mod._TOOLS_LRU.save(lru)

    def test_evicts_least_recently_used_over_limit(self):
        # Limit is 3; seed 4 tools with explicit timestamps; oldest must go.
        self._seed("a", ts=100.0)
        self._seed("b", ts=200.0)
        self._seed("c", ts=300.0)
        self._seed("d", ts=400.0)
        evicted = tools_mod._evict_tools_if_needed()
        self.assertEqual(evicted, ["a.py"])
        self.assertFalse((self.tools_dir / "a.py").exists())
        self.assertTrue((self.tools_dir / "d.py").exists())

    def test_no_eviction_when_under_limit(self):
        for name in ("a", "b"):
            self._seed(name, ts=100.0)
        self.assertEqual(tools_mod._evict_tools_if_needed(), [])
        self.assertTrue((self.tools_dir / "a.py").exists())

    def test_call_bumps_lru_timestamp(self):
        self._seed("a", ts=100.0)
        # Load + dispatch 'a' — handler is wrapped to touch.
        _schemas, dispatch, _warnings = tools_mod.load_dynamic_tools()
        dispatch["a"]()
        lru = tools_mod._TOOLS_LRU.load()
        self.assertGreater(lru["a.py"], 100.0)

    def test_load_evicts_before_returning_dispatch(self):
        # Seed 4 tools (over the limit of 3); load should evict the oldest first.
        self._seed("a", ts=100.0)
        self._seed("b", ts=200.0)
        self._seed("c", ts=300.0)
        self._seed("d", ts=400.0)
        schemas, dispatch, warnings = tools_mod.load_dynamic_tools()
        self.assertNotIn("a", dispatch)
        self.assertEqual({"b", "c", "d"}, set(dispatch))
        self.assertTrue(any("evicted" in w for w in warnings))


class MemoryLRUTests(unittest.TestCase):
    """Memory LRU: capped total across categories, evict oldest on add, touch on select."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.mem = self.ws / "memory"
        self._patches = [
            patch.object(config, "WORKSPACE", self.ws),
            patch.object(config, "MEMORY_DIR", self.mem),
            patch.object(memory, "MEMORY_LRU_LIMIT", 3),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    @staticmethod
    def _l(desc: str) -> dict:
        return {"description": desc, "prompt": f"FULL: {desc}"}

    def _stamp(self, catalog_id: str, ts: float) -> None:
        lru = memory._MEMORY_LRU.load()
        lru[catalog_id] = ts
        memory._MEMORY_LRU.save(lru)

    def test_add_evicts_oldest_when_over_limit(self):
        # Seed 3 recipes under the cap, manually backdate them.
        memory.append_memory({"recipes": [self._l("first"), self._l("second"), self._l("third")]})
        self._stamp("recipes:first", 100.0)
        self._stamp("recipes:second", 200.0)
        self._stamp("recipes:third", 300.0)
        # Adding a 4th triggers eviction of the oldest ('first').
        memory.append_memory({"recipes": [self._l("fourth")]})
        ids = sorted(e["id"] for e in memory.load_index("recipes"))
        self.assertEqual(ids, ["fourth", "second", "third"])
        self.assertFalse((self.mem / "recipes" / "first.py").exists())

    def test_select_bumps_lru_timestamp_for_selected_recipes(self):
        memory.append_memory({"recipes": [self._l("ls first")]})
        self._stamp("recipes:ls_first", 100.0)
        response = fake_response(
            tool_calls=[fake_tool_call("c", "select_memories", {"ids": ["recipes:ls_first"]})],
            finish_reason="tool_calls",
        )
        with patch.object(config, "completion", return_value=response):
            memory.select_relevant_memories("a task")
        lru = memory._MEMORY_LRU.load()
        self.assertGreater(lru["recipes:ls_first"], 100.0)

    def test_no_eviction_when_under_limit(self):
        memory.append_memory({"recipes": [self._l("only one")]})
        # No eviction; recipe still present.
        self.assertEqual([e["id"] for e in memory.load_index("recipes")], ["only_one"])


class SameResponseToolReloadTest(unittest.TestCase):
    """A tool written and called in the same API response should succeed."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.tools_dir = self.ws / "tools"
        self.tools_dir.mkdir()
        self._patches = [
            patch.object(config, "WORKSPACE", self.ws),
            patch.object(config, "TOOLS_DIR", self.tools_dir),
            patch.object(config, "MEMORY_DIR", self.ws / "memory"),
            patch.object(config, "DONE_MARKER", self.ws / "DONE"),
            patch.object(config, "bus", None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_write_then_call_in_same_response(self):
        tool_source = (
            'TOOL = {"name": "shout", "description": "x", '
            '"input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, '
            '"required": ["text"]}}\n'
            'def run(text): return {"shouted": text.upper()}\n'
        )
        first = fake_response(
            tool_calls=[
                fake_tool_call("tu_1", "write", {"path": "tools/shout.py", "content": tool_source}),
                fake_tool_call("tu_2", "shout", {"text": "hi"}),
            ],
            finish_reason="tool_calls",
        )
        second = fake_response(content="done", finish_reason="stop")

        with patch.object(config, "completion", side_effect=[first, second]):
            messages = ralph.run_iteration("task", 1)

        # Find tool-result messages; assert tu_2 actually ran.
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        by_id = {m["tool_call_id"]: m["content"] for m in tool_msgs}
        self.assertIn("tu_2", by_id)
        self.assertNotIn("unknown tool", by_id["tu_2"])
        self.assertIn("HI", by_id["tu_2"])


class PeersPreambleTests(unittest.TestCase):
    """Each iteration starts with a `## Peers on the bus` section listing other ralphs' inventories."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.tools_dir = self.ws / "tools"
        self.mem_dir = self.ws / "memory"
        self.tools_dir.mkdir()
        self.mem_dir.mkdir()
        self._patches = [
            patch.object(config, "WORKSPACE", self.ws),
            patch.object(config, "TOOLS_DIR", self.tools_dir),
            patch.object(config, "MEMORY_DIR", self.mem_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_list_my_tools_skips_underscore_prefixed(self):
        (self.tools_dir / "shout.py").write_text("# x")
        (self.tools_dir / "_lru.py").write_text("LRU = {}")
        self.assertEqual(ralph.list_my_tools(), ["shout.py"])

    def test_list_my_memory_summarizes_indexes(self):
        memory.append_memory({
            "recipes": [{"description": "argparse flow", "prompt": "..."}],
        })
        out = ralph.list_my_memory()
        self.assertEqual(out["recipes"][0]["description"], "argparse flow")
        # No worked/failed keys anymore — single-category memory.
        self.assertNotIn("worked", out)
        self.assertNotIn("failed", out)

    def test_format_peers_preamble_empty_when_no_bus(self):
        with patch.object(config, "bus", None):
            self.assertEqual(ralph.format_peers_preamble(), "")

    def test_format_peers_preamble_skips_self(self):
        mock_bus = MagicMock()
        mock_bus.id = "me"
        mock_bus.list_ralphs.return_value = ["me"]
        with patch.object(config, "bus", mock_bus):
            self.assertEqual(ralph.format_peers_preamble(), "")
        mock_bus.peek_ralph.assert_not_called()

    def test_format_peers_preamble_renders_peer_inventory(self):
        mock_bus = MagicMock()
        mock_bus.id = "me"
        mock_bus.list_ralphs.return_value = ["me", "expert"]
        mock_bus.peek_ralph.return_value = {
            "id": "expert", "iteration": 5, "done": True,
            "last_text": "marked done after solver passed tests.",
            "tools": ["sudoku_solver.py", "validate.py"],
            "memory": {
                "recipes": [
                    {"id": "constraint_prop", "description": "constraint propagation across rows/cols/boxes"},
                ],
            },
        }
        with patch.object(config, "bus", mock_bus):
            preamble = ralph.format_peers_preamble()
        self.assertTrue(preamble.startswith("## Peers on the bus"))
        self.assertIn("### expert", preamble)
        self.assertIn("iteration=5", preamble)
        self.assertIn("done=True", preamble)
        self.assertIn("sudoku_solver.py", preamble)
        self.assertIn("constraint propagation", preamble)
        self.assertNotIn("### me", preamble)

    def test_format_peers_preamble_caps_descriptions(self):
        mock_bus = MagicMock()
        mock_bus.id = "me"
        mock_bus.list_ralphs.return_value = ["me", "verbose"]
        mock_bus.peek_ralph.return_value = {
            "id": "verbose", "iteration": 1, "done": False, "last_text": "",
            "tools": [],
            "memory": {
                "recipes": [{"id": f"r{i}", "description": f"recipe {i}"} for i in range(15)],
            },
        }
        with patch.object(config, "bus", mock_bus):
            preamble = ralph.format_peers_preamble()
        self.assertIn("recipes: 15", preamble)
        self.assertIn("(+5 more)", preamble)

    def test_format_peers_preamble_surfaces_expertise(self):
        mock_bus = MagicMock()
        mock_bus.id = "me"
        mock_bus.list_ralphs.return_value = ["me", "expert"]
        mock_bus.peek_ralph.return_value = {
            "id": "expert", "iteration": 2, "done": False, "last_text": "",
            "expertise": "Sudoku solving",
            "tools": [], "memory": {"recipes": []},
        }
        with patch.object(config, "bus", mock_bus):
            preamble = ralph.format_peers_preamble()
        self.assertIn("expertise: Sudoku solving", preamble)

    def test_format_peers_preamble_omits_expertise_when_missing(self):
        mock_bus = MagicMock()
        mock_bus.id = "me"
        mock_bus.list_ralphs.return_value = ["me", "anon"]
        mock_bus.peek_ralph.return_value = {
            "id": "anon", "iteration": 1, "done": False, "last_text": "",
            "tools": [], "memory": {"recipes": []},
        }
        with patch.object(config, "bus", mock_bus):
            preamble = ralph.format_peers_preamble()
        self.assertNotIn("expertise:", preamble)


class WaitUntilDoneRemovedTest(unittest.TestCase):
    """The idle phase blocks until the DONE marker is deleted."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.done = self.ws / "DONE"
        self.done.write_text("done")
        self._patch = patch.object(config, "DONE_MARKER", self.done)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_returns_when_marker_removed(self):
        def remove_after_delay():
            time.sleep(0.2)
            self.done.unlink()
        threading.Thread(target=remove_after_delay, daemon=True).start()

        start = time.time()
        ralph.wait_until_done_removed(poll_seconds=0.05)
        elapsed = time.time() - start
        self.assertGreater(elapsed, 0.1)
        self.assertLess(elapsed, 2.0)
        self.assertFalse(self.done.exists())

    def test_returns_immediately_if_marker_already_gone(self):
        self.done.unlink()
        start = time.time()
        ralph.wait_until_done_removed(poll_seconds=1.0)
        # Should return without waiting a full poll cycle.
        self.assertLess(time.time() - start, 0.5)


class BusTests(unittest.TestCase):
    """Inter-Ralph bus: status, observe, ask/respond round-trip via FIFO."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.bus_dir = Path(self._tmp.name)
        self._buses: list[Bus] = []

    def tearDown(self):
        for b in self._buses:
            b.close()
        self._tmp.cleanup()

    def _make(self, ralph_id: str) -> Bus:
        b = Bus(self.bus_dir, ralph_id)
        self._buses.append(b)
        return b

    def test_status_roundtrip(self):
        a = self._make("alpha")
        a.write_status({"id": "alpha", "iteration": 3, "done": False})
        b = self._make("beta")
        self.assertEqual(set(b.list_ralphs()), {"alpha"})
        peek = b.peek_ralph("alpha")
        self.assertEqual(peek["iteration"], 3)
        self.assertFalse(peek["done"])

    def test_peek_unknown_ralph_returns_error(self):
        b = self._make("alpha")
        self.assertIn("error", b.peek_ralph("ghost"))

    def test_ask_target_not_running(self):
        a = self._make("alpha")
        result = a.ask("ghost", "tools")
        self.assertIn("error", result)

    def test_ask_then_manual_respond_roundtrip(self):
        """Manual respond path: used when no fulfillment callback is registered."""
        a = self._make("alpha")
        b = self._make("beta")  # no fulfill_request → falls back to _pending queue
        ask_result = a.ask("beta", "tools")
        self.assertEqual(ask_result["status"], "sent")
        rid = ask_result["request_id"]

        deadline = time.time() + 2.0
        pending: list[dict] = []
        while time.time() < deadline:
            pending = b.drain_pending()
            if pending:
                break
            time.sleep(0.05)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["category"], "tools")
        self.assertEqual(pending[0]["from"], "alpha")
        self.assertEqual(a.check_response(rid)["status"], "pending")

        b.respond(rid, {"files": {"shout.py": "..."}})
        resp = a.check_response(rid)
        self.assertEqual(resp["answer"], {"files": {"shout.py": "..."}})
        self.assertEqual(resp["from"], "beta")

    def test_ask_with_fulfillment_callback_auto_responds(self):
        """When fulfill_request is set, the listener responds without manual intervention."""
        calls: list[tuple] = []

        def fulfill(category, request):
            calls.append((category, request["from"]))
            return {"echoed": category}

        a = self._make("alpha")
        b = Bus(self.bus_dir, "beta", fulfill_request=fulfill)
        self._buses.append(b)

        ask_result = a.ask("beta", "recipes")
        rid = ask_result["request_id"]

        deadline = time.time() + 2.0
        resp: dict = {"status": "pending"}
        while time.time() < deadline and resp.get("status") == "pending":
            resp = a.check_response(rid)
            if resp.get("status") == "pending":
                time.sleep(0.05)
        self.assertEqual(resp["answer"], {"echoed": "recipes"})
        self.assertEqual(calls, [("recipes", "alpha")])
        # Auto-fulfill should not queue requests.
        self.assertEqual(b.drain_pending(), [])

    def test_drain_pending_empty_when_idle(self):
        a = self._make("alpha")
        self.assertEqual(a.drain_pending(), [])

    def test_close_removes_fifo(self):
        a = Bus(self.bus_dir, "ephemeral")
        fifo = a.fifo_path
        self.assertTrue(fifo.exists())
        a.close()
        self.assertFalse(fifo.exists())


class FulfillPeerRequestTests(unittest.TestCase):
    """The auto-fulfillment function reads our own filesystem artifacts."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.tools_dir = self.ws / "tools"
        self.mem_dir = self.ws / "memory"
        self.tools_dir.mkdir()
        self.mem_dir.mkdir()
        self._patches = [
            patch.object(config, "WORKSPACE", self.ws),
            patch.object(config, "TOOLS_DIR", self.tools_dir),
            patch.object(config, "MEMORY_DIR", self.mem_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    @staticmethod
    def _tool_source(name: str, description: str) -> str:
        return (
            f'TOOL = {{"name": "{name}", "description": "{description}", '
            f'"input_schema": {{"type": "object", "properties": {{}}}}}}\n'
            f'def run(**kwargs):\n    return "ok"\n'
        )

    def test_tools_returns_top_k_by_description_match(self):
        (self.tools_dir / "sudoku_solver.py").write_text(
            self._tool_source("sudoku_solver", "Solve sudoku puzzles via backtracking"))
        (self.tools_dir / "shout.py").write_text(
            self._tool_source("shout", "Uppercase the input text"))
        (self.tools_dir / "_helper.py").write_text("# private")
        (self.tools_dir / "notpy.txt").write_text("ignored")
        result = ralph.fulfill_peer_request(
            "tools", {"from": "x", "description": "need a sudoku solver"},
        )
        # Description-matched tool ranks first; private files and non-py files are excluded.
        self.assertIn("sudoku_solver.py", result["files"])
        self.assertNotIn("_helper.py", result["files"])
        self.assertNotIn("notpy.txt", result["files"])

    def test_tools_caps_at_top_k(self):
        for i in range(5):
            (self.tools_dir / f"t{i}.py").write_text(
                self._tool_source(f"t{i}", f"tool number {i}"))
        result = ralph.fulfill_peer_request(
            "tools", {"from": "x", "description": "anything"},
        )
        self.assertLessEqual(len(result["files"]), ralph.PEER_ASK_TOP_K)

    def test_tools_empty_when_dir_missing(self):
        import shutil
        shutil.rmtree(self.tools_dir)
        result = ralph.fulfill_peer_request(
            "tools", {"from": "x", "description": "anything"},
        )
        self.assertEqual(result, {"files": {}})

    def test_recipes_ranked_by_description_match(self):
        memory.append_memory({
            "recipes": [
                {"description": "ls first", "prompt": "always call ls"},
                {"description": "edit surgical", "prompt": "prefer edit over write"},
                {"description": "use grep for finding code", "prompt": "grep beats find"},
                {"description": "test before commit", "prompt": "run tests"},
            ],
        })
        result = ralph.fulfill_peer_request(
            "recipes", {"from": "x", "description": "searching for code with grep"},
        )
        # The grep recipe should rank first since its description shares tokens with the query.
        self.assertGreater(len(result["lessons"]), 0)
        self.assertEqual(result["lessons"][0]["id"], "use_grep_for_finding_code")
        # And we never return more than top-K.
        self.assertLessEqual(len(result["lessons"]), ralph.PEER_ASK_TOP_K)

    def test_recipes_returns_full_prompt(self):
        memory.append_memory({
            "recipes": [{"description": "ls first", "prompt": "always call ls"}],
        })
        result = ralph.fulfill_peer_request(
            "recipes", {"from": "x", "description": "ls"},
        )
        lesson = result["lessons"][0]
        self.assertEqual(lesson["description"], "ls first")
        self.assertEqual(lesson["prompt"], "always call ls")

    def test_recipes_empty_when_no_lessons(self):
        result = ralph.fulfill_peer_request(
            "recipes", {"from": "x", "description": "anything"},
        )
        self.assertEqual(result, {"lessons": []})

    def test_unknown_category_returns_error(self):
        result = ralph.fulfill_peer_request(
            "question", {"from": "x", "description": "anything"},
        )
        self.assertIn("error", result)
        self.assertIn("unknown category", result["error"])


class CheckResponseAutoInstallTests(unittest.TestCase):
    """check_response auto-installs peer 'tools' bundles into the local TOOLS_DIR,
    so source bytes never round-trip through the LLM (which would re-format them)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tools_dir = Path(self._tmp.name) / "tools"
        self.tools_dir.mkdir()
        self._patches = [
            patch.object(config, "TOOLS_DIR", self.tools_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _call(self, request_id: str = "abc") -> dict:
        return json.loads(tools_mod.handle_tool(
            "check_response", {"request_id": request_id}, {},
        ))

    def test_pending_response_passes_through(self):
        mock_bus = MagicMock()
        mock_bus.check_response.return_value = {"request_id": "abc", "status": "pending"}
        with patch.object(config, "bus", mock_bus):
            result = self._call()
        self.assertEqual(result, {"request_id": "abc", "status": "pending"})

    def test_tools_answer_auto_installs_verbatim(self):
        # The exact formatting (newlines, indentation, comments) must survive end-to-end.
        source = (
            'TOOL = {\n'
            '    "name": "sudoku_solver",\n'
            '    "description": "Solve a 9x9 sudoku given as 81 digits.",\n'
            '    "input_schema": {"type": "object", "properties": {"grid": {"type": "string"}}},\n'
            '}\n'
            '\n'
            'def run(grid: str) -> dict:\n'
            '    # carefully crafted comment that must not be lost\n'
            '    return {"solved": grid}\n'
        )
        mock_bus = MagicMock()
        mock_bus.check_response.return_value = {
            "request_id": "abc", "from": "expert",
            "answer": {"files": {"sudoku_solver.py": source}},
            "ts": 0,
        }
        with patch.object(config, "bus", mock_bus):
            result = self._call()
        installed = (self.tools_dir / "sudoku_solver.py").read_text()
        self.assertEqual(installed, source)  # byte-for-byte
        self.assertEqual(result["installed_tools"][0]["filename"], "sudoku_solver.py")
        self.assertEqual(result["installed_tools"][0]["bytes"], len(source.encode()))
        self.assertNotIn("files", result)  # raw source NOT echoed back to the LLM

    def test_lessons_answer_passes_through_unchanged(self):
        lessons = [{"id": "abc", "description": "x", "prompt": "do y"}]
        mock_bus = MagicMock()
        mock_bus.check_response.return_value = {
            "request_id": "abc", "from": "expert",
            "answer": {"lessons": lessons},
            "ts": 0,
        }
        with patch.object(config, "bus", mock_bus):
            result = self._call()
        self.assertEqual(result["answer"]["lessons"], lessons)

    def test_strips_path_components_in_filename(self):
        # A careless or hostile peer can't escape TOOLS_DIR via a relative path.
        mock_bus = MagicMock()
        mock_bus.check_response.return_value = {
            "request_id": "abc", "from": "expert",
            "answer": {"files": {"../../etc/passwd.py": "EVIL = True"}},
            "ts": 0,
        }
        with patch.object(config, "bus", mock_bus):
            self._call()
        self.assertTrue((self.tools_dir / "passwd.py").exists())
        # Did not write outside TOOLS_DIR.
        escaped = self.tools_dir.parent.parent / "etc" / "passwd.py"
        self.assertFalse(escaped.exists())

    def test_installed_tool_is_loadable_on_next_refresh(self):
        """The whole point of auto-install: after check_response writes the file,
        load_dynamic_tools() must pick it up so the model can call it directly on
        the next tool_use. If this regressed, the LLM would invent some tool_use
        wrapper around the missing name."""
        source = (
            'TOOL = {"name": "solve_sudoku", '
            '"description": "Solve a sudoku", '
            '"input_schema": {"type": "object", "properties": {"grid": {"type": "string"}}}}\n'
            'def run(grid):\n    return {"solved": grid}\n'
        )
        mock_bus = MagicMock()
        mock_bus.check_response.return_value = {
            "request_id": "abc", "from": "expert",
            "answer": {"files": {"solve_sudoku.py": source}},
            "ts": 0,
        }
        with patch.object(config, "bus", mock_bus):
            self._call()
        # Now simulate the run_iteration reload: load_dynamic_tools should see the new tool.
        schemas, dispatch, _warnings = tools_mod.load_dynamic_tools()
        names = [s["name"] for s in schemas]
        self.assertIn("solve_sudoku", names)
        self.assertIn("solve_sudoku", dispatch)
        # Sanity: the tool actually runs.
        result = dispatch["solve_sudoku"](grid="123")
        self.assertEqual(result, {"solved": "123"})

    def test_empty_files_dict_gives_honest_message(self):
        """When the peer returns {files: {}} (e.g., they haven't built any tools yet),
        the note must NOT claim 'Tools were auto-installed' and must steer the model
        away from polling the same request_id forever."""
        mock_bus = MagicMock()
        mock_bus.check_response.return_value = {
            "request_id": "abc", "from": "expert",
            "answer": {"files": {}},
            "ts": 0,
        }
        with patch.object(config, "bus", mock_bus):
            result = self._call()
        self.assertEqual(result["installed_tools"], [])
        # Note must NOT lie about an install having happened…
        self.assertNotIn("auto-installed", result["note"])
        # …and must steer the agent toward something other than re-polling.
        self.assertIn("Do NOT keep calling check_response", result["note"])

    def test_skips_non_py_and_private_files(self):
        mock_bus = MagicMock()
        mock_bus.check_response.return_value = {
            "request_id": "abc", "from": "expert",
            "answer": {"files": {
                "good.py": "TOOL = {}; def run(): pass",
                "_private.py": "skipped — leading underscore",
                "notes.txt": "skipped — wrong extension",
            }},
            "ts": 0,
        }
        with patch.object(config, "bus", mock_bus):
            result = self._call()
        installed_names = [t["filename"] for t in result["installed_tools"]]
        self.assertEqual(installed_names, ["good.py"])
        self.assertFalse((self.tools_dir / "_private.py").exists())
        self.assertFalse((self.tools_dir / "notes.txt").exists())


class AskRalphValidationTests(unittest.TestCase):
    """The ask_ralph builtin validates category + description before hitting the bus."""

    def test_rejects_unknown_category(self):
        with patch.object(config, "bus", MagicMock()) as mock_bus:
            result = json.loads(tools_mod.handle_tool(
                "ask_ralph",
                {"id": "beta", "category": "anything", "description": "need help"},
                {},
            ))
            self.assertIn("error", result)
            self.assertIn("category must be one of", result["error"])
            mock_bus.ask.assert_not_called()

    def test_rejects_empty_description(self):
        with patch.object(config, "bus", MagicMock()) as mock_bus:
            result = json.loads(tools_mod.handle_tool(
                "ask_ralph",
                {"id": "beta", "category": "tools", "description": "   "},
                {},
            ))
            self.assertIn("error", result)
            self.assertIn("description", result["error"])
            mock_bus.ask.assert_not_called()

    def test_passes_category_and_description_to_bus(self):
        mock_bus = MagicMock()
        mock_bus.ask.return_value = {"request_id": "abc", "status": "sent"}
        with patch.object(config, "bus", mock_bus):
            result = json.loads(tools_mod.handle_tool(
                "ask_ralph",
                {"id": "beta", "category": "tools", "description": "sudoku solver"},
                {},
            ))
        self.assertEqual(result, {"request_id": "abc", "status": "sent"})
        mock_bus.ask.assert_called_once_with("beta", "tools", "sudoku solver")


class WaitForPeersTests(unittest.TestCase):
    """wait_for_peers polls config.bus.peek_ralph until each named peer reports done=True."""

    def test_returns_immediately_for_empty_list(self):
        # Should not touch the bus at all when given nothing to wait for.
        with patch.object(config, "bus", MagicMock()) as mock_bus:
            ralph.wait_for_peers([], poll_seconds=0.01)
        mock_bus.peek_ralph.assert_not_called()

    def test_returns_once_peer_marks_done(self):
        mock_bus = MagicMock()
        mock_bus.peek_ralph.side_effect = [
            {"id": "expert", "done": False},
            {"id": "expert", "done": False},
            {"id": "expert", "done": True},
        ]
        with patch.object(config, "bus", mock_bus):
            ralph.wait_for_peers(["expert"], poll_seconds=0.01)
        self.assertEqual(mock_bus.peek_ralph.call_count, 3)

    def test_treats_missing_status_as_not_yet_ready(self):
        # peek_ralph returning an error dict (peer hasn't joined the bus) is not fatal —
        # just keep polling.
        mock_bus = MagicMock()
        mock_bus.peek_ralph.side_effect = [
            {"error": "no status for ralph: expert"},
            {"id": "expert", "done": True},
        ]
        with patch.object(config, "bus", mock_bus):
            ralph.wait_for_peers(["expert"], poll_seconds=0.01)
        self.assertEqual(mock_bus.peek_ralph.call_count, 2)

    def test_waits_for_all_listed_peers(self):
        # Two peers — both must report done before wait returns.
        mock_bus = MagicMock()
        responses = {
            "alpha": [{"done": False}, {"done": True}, {"done": True}],
            "beta":  [{"done": False}, {"done": False}, {"done": True}],
        }
        def peek(pid):
            return responses[pid].pop(0)
        mock_bus.peek_ralph.side_effect = peek
        with patch.object(config, "bus", mock_bus):
            ralph.wait_for_peers(["alpha", "beta"], poll_seconds=0.01)
        # Total of 5 peek calls: 2 in pass 1, 1 in pass 2 (alpha now ready, only beta polled),
        # 1 more in pass 3 (beta still not ready), 1 in pass 4 (beta done).
        # Actually: pass 1 sees both = 2 calls; alpha ready, removed; passes 2,3,4 each poll only
        # beta = 3 calls; total 5.
        self.assertEqual(mock_bus.peek_ralph.call_count, 5)


class ValidateModelTests(unittest.TestCase):
    """validate_model() smoke-tests the configured model and exits on misconfiguration."""

    def _make_litellm_error(self, exc_class):
        # litellm exceptions need (message, model, llm_provider) at minimum.
        return exc_class(message="boom", model=config.MODEL, llm_provider="test")

    def test_success_does_not_exit(self):
        with patch.object(config, "completion", return_value=MagicMock()):
            ralph.validate_model()  # must not raise SystemExit

    def test_uses_forced_tool_choice(self):
        """The ping call MUST use forced tool_choice — that's the capability we
        actually need from any model Ralph runs against (memory + expertise both
        depend on it). A plain completion check would let through models that
        accept completions but reject forced tool_choice."""
        mock_completion = MagicMock(return_value=MagicMock())
        with patch.object(config, "completion", mock_completion):
            ralph.validate_model()
        call_kwargs = mock_completion.call_args.kwargs
        self.assertIn("tools", call_kwargs)
        self.assertIn("tool_choice", call_kwargs)
        self.assertEqual(call_kwargs["tool_choice"]["type"], "function")
        self.assertEqual(call_kwargs["tool_choice"]["function"]["name"], "ping")

    def test_exits_on_bad_request(self):
        import litellm
        err = self._make_litellm_error(litellm.BadRequestError)
        with patch.object(config, "completion", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                ralph.validate_model()
        self.assertEqual(cm.exception.code, 1)

    def test_exits_on_auth_error(self):
        import litellm
        err = self._make_litellm_error(litellm.AuthenticationError)
        with patch.object(config, "completion", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                ralph.validate_model()
        self.assertEqual(cm.exception.code, 1)

    def test_exits_on_not_found(self):
        import litellm
        err = self._make_litellm_error(litellm.NotFoundError)
        with patch.object(config, "completion", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                ralph.validate_model()
        self.assertEqual(cm.exception.code, 1)

    def test_transient_error_is_not_fatal(self):
        # Rate-limit-style errors should NOT exit — the main loop will retry.
        with patch.object(config, "completion", side_effect=RuntimeError("rate limit")):
            ralph.validate_model()  # must not raise SystemExit


if __name__ == "__main__":
    unittest.main()
