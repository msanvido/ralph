"""Tests. Run with: python -m unittest test_ralph"""

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import config
import memory
import ralph
import tools as tools_mod
from bus import Bus

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
            "worked": [self._l("call ls before reading"), self._l("use edit for surgical changes")],
            "failed": [self._l("called a tool in the same response that created it")],
            "recipes": [],
        })
        self.assertEqual(count, 3)
        worked_dir = self.mem / "worked"
        self.assertTrue((worked_dir / "_index.py").exists())
        self.assertTrue((worked_dir / "call_ls_before_reading.py").exists())
        self.assertTrue((worked_dir / "use_edit_for_surgical_changes.py").exists())
        self.assertFalse((self.mem / "recipes").exists())
        # Lesson file holds both fields.
        lesson = memory.load_lesson("worked", "call_ls_before_reading")
        self.assertEqual(lesson["description"], "call ls before reading")
        self.assertEqual(lesson["prompt"], "FULL: call ls before reading")
        # Index lists the lesson.
        index = memory.load_index("worked")
        self.assertEqual(
            sorted(e["id"] for e in index),
            ["call_ls_before_reading", "use_edit_for_surgical_changes"],
        )

    def test_append_memory_skips_empty_or_malformed_entries(self):
        count = memory.append_memory({
            "worked": [
                {"description": "", "prompt": "x"},
                {"description": "x", "prompt": ""},
                "not-a-dict",
                None,
                {"description": "valid", "prompt": "valid prompt"},
            ],
            "failed": [],
            "recipes": [],
        })
        self.assertEqual(count, 1)
        self.assertEqual([e["id"] for e in memory.load_index("worked")], ["valid"])

    def test_append_memory_disambiguates_colliding_slugs(self):
        # Two descriptions slugify to the same id — second should get a _2 suffix.
        memory.append_memory({"worked": [self._l("Use ls!")], "failed": [], "recipes": []})
        memory.append_memory({"worked": [self._l("use ls?")], "failed": [], "recipes": []})
        ids = [e["id"] for e in memory.load_index("worked")]
        self.assertEqual(ids, ["use_ls", "use_ls_2"])
        self.assertTrue((self.mem / "worked" / "use_ls.py").exists())
        self.assertTrue((self.mem / "worked" / "use_ls_2.py").exists())

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
            "worked": [self._l("call ls first"), self._l("use edit")],
            "failed": [self._l("tool same response")],
            "recipes": [],
        })
        items = memory.read_memory_items()
        ids = [i[0] for i in items]
        self.assertIn("worked:call_ls_first", ids)
        self.assertIn("worked:use_edit", ids)
        self.assertIn("failed:tool_same_response", ids)
        # The third element is the (short) description — not the full prompt.
        descriptions = {i[0]: i[2] for i in items}
        self.assertEqual(descriptions["worked:call_ls_first"], "call ls first")

    def test_select_returns_empty_when_no_memory(self):
        with patch.object(config, "completion") as mock:
            self.assertEqual(memory.select_relevant_memories("any task"), "")
            mock.assert_not_called()

    def test_select_loads_full_prompts_for_chosen_ids(self):
        memory.append_memory({
            "worked": [
                {"description": "ls first", "prompt": "FULL ls prompt"},
                {"description": "use edit", "prompt": "FULL edit prompt"},
            ],
            "failed": [{"description": "same response", "prompt": "FULL fail prompt"}],
            "recipes": [{"description": "argparse flow", "prompt": "FULL recipe prompt"}],
        })
        response = fake_response(
            tool_calls=[fake_tool_call("c", "select_memories", {
                "ids": ["worked:ls_first", "recipes:argparse_flow"],
            })],
            finish_reason="tool_calls",
        )
        with patch.object(config, "completion", return_value=response):
            preamble = memory.select_relevant_memories("write a CLI flag")
        # Selected lessons appear via their full prompt (not just description).
        self.assertIn("FULL ls prompt", preamble)
        self.assertIn("FULL recipe prompt", preamble)
        self.assertNotIn("FULL edit prompt", preamble)
        self.assertNotIn("FULL fail prompt", preamble)
        self.assertIn("### worked", preamble)
        self.assertIn("### recipes", preamble)
        self.assertNotIn("### failed", preamble)

    def test_select_catalog_uses_descriptions_not_prompts(self):
        memory.append_memory({
            "worked": [{"description": "SHORT desc", "prompt": "LONG prompt body here"}],
            "failed": [], "recipes": [],
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
        self.assertIn("worked:short_desc", user_content)
        self.assertIn("SHORT desc", user_content)
        # The full prompt body should NOT be in the selection catalog.
        self.assertNotIn("LONG prompt body", user_content)

    def test_select_swallows_api_errors(self):
        memory.append_memory({"worked": [self._l("x")], "failed": [], "recipes": []})
        with patch.object(config, "completion", side_effect=RuntimeError("boom")):
            self.assertEqual(memory.select_relevant_memories("task"), "")

    def test_learn_from_iteration_skips_empty(self):
        with patch.object(config, "completion") as mock:
            memory.learn_from_iteration([{"role": "user", "content": "hi"}], 1)
            mock.assert_not_called()

    def test_learn_from_iteration_writes_lesson_files(self):
        response = fake_response(
            tool_calls=[fake_tool_call("c", "record_learnings", {
                "worked": [{"description": "ls first", "prompt": "Always call ls before reading."}],
                "failed": [],
                "recipes": [{"description": "use edit", "prompt": "Use edit for surgical changes."}],
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
        worked = memory.load_lesson("worked", "ls_first")
        self.assertEqual(worked["description"], "ls first")
        self.assertIn("Always call ls", worked["prompt"])
        recipe = memory.load_lesson("recipes", "use_edit")
        self.assertEqual(recipe["description"], "use edit")
        self.assertIn("Use edit for surgical", recipe["prompt"])
        # No 'failed' lessons → no failed/ directory.
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
        # Seed 3 lessons under the cap, manually backdate them.
        memory.append_memory({"worked": [self._l("first"), self._l("second"), self._l("third")],
                              "failed": [], "recipes": []})
        self._stamp("worked:first", 100.0)
        self._stamp("worked:second", 200.0)
        self._stamp("worked:third", 300.0)
        # Adding a 4th triggers eviction of the oldest ('first').
        memory.append_memory({"worked": [self._l("fourth")], "failed": [], "recipes": []})
        ids = sorted(e["id"] for e in memory.load_index("worked"))
        self.assertEqual(ids, ["fourth", "second", "third"])
        self.assertFalse((self.mem / "worked" / "first.py").exists())

    def test_eviction_works_across_categories(self):
        # 1 in worked, 1 in failed, 1 in recipes — total 3, at the cap.
        memory.append_memory({
            "worked": [self._l("w1")], "failed": [self._l("f1")], "recipes": [self._l("r1")],
        })
        self._stamp("worked:w1", 100.0)
        self._stamp("failed:f1", 200.0)
        self._stamp("recipes:r1", 300.0)
        # Adding one more should evict 'worked:w1' (oldest).
        memory.append_memory({"worked": [self._l("w2")], "failed": [], "recipes": []})
        all_ids = {cid for cid, _, _ in memory.read_memory_items()}
        self.assertEqual(all_ids, {"worked:w2", "failed:f1", "recipes:r1"})

    def test_select_bumps_lru_timestamp_for_selected_lessons(self):
        memory.append_memory({"worked": [self._l("ls first")], "failed": [], "recipes": []})
        self._stamp("worked:ls_first", 100.0)
        response = fake_response(
            tool_calls=[fake_tool_call("c", "select_memories", {"ids": ["worked:ls_first"]})],
            finish_reason="tool_calls",
        )
        with patch.object(config, "completion", return_value=response):
            memory.select_relevant_memories("a task")
        lru = memory._MEMORY_LRU.load()
        self.assertGreater(lru["worked:ls_first"], 100.0)

    def test_no_eviction_when_under_limit(self):
        memory.append_memory({"worked": [self._l("only one")], "failed": [], "recipes": []})
        # No eviction; lesson still present.
        self.assertEqual([e["id"] for e in memory.load_index("worked")], ["only_one"])


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
            "worked": [{"description": "ls first", "prompt": "..."}],
            "failed": [],
            "recipes": [{"description": "argparse flow", "prompt": "..."}],
        })
        out = ralph.list_my_memory()
        self.assertEqual(out["worked"][0]["description"], "ls first")
        self.assertEqual(out["recipes"][0]["description"], "argparse flow")
        self.assertEqual(out["failed"], [])

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
                "worked": [
                    {"id": "mrv_heuristic", "description": "MRV picks the most-constrained cell"},
                ],
                "failed": [],
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
        self.assertIn("MRV picks the most-constrained cell", preamble)
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
                "worked": [{"id": f"l{i}", "description": f"lesson {i}"} for i in range(15)],
                "failed": [], "recipes": [],
            },
        }
        with patch.object(config, "bus", mock_bus):
            preamble = ralph.format_peers_preamble()
        self.assertIn("worked: 15", preamble)
        self.assertIn("(+5 more)", preamble)


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

    def test_tools_returns_all_python_files(self):
        (self.tools_dir / "shout.py").write_text("# shout source")
        (self.tools_dir / "_helper.py").write_text("# private")
        (self.tools_dir / "notpy.txt").write_text("ignored")
        result = ralph.fulfill_peer_request("tools", {"from": "x"})
        self.assertIn("shout.py", result["files"])
        self.assertNotIn("_helper.py", result["files"])
        self.assertNotIn("notpy.txt", result["files"])
        self.assertEqual(result["files"]["shout.py"], "# shout source")

    def test_tools_empty_when_dir_missing(self):
        import shutil
        shutil.rmtree(self.tools_dir)
        result = ralph.fulfill_peer_request("tools", {"from": "x"})
        self.assertEqual(result, {"files": {}})

    def test_memory_category_returns_lessons(self):
        memory.append_memory({
            "worked": [
                {"description": "ls first", "prompt": "always call ls"},
                {"description": "edit surgical", "prompt": "prefer edit over write"},
            ],
            "failed": [], "recipes": [],
        })
        result = ralph.fulfill_peer_request("worked", {"from": "x"})
        ids = sorted(l["id"] for l in result["lessons"])
        self.assertEqual(ids, ["edit_surgical", "ls_first"])
        ls_first = next(l for l in result["lessons"] if l["id"] == "ls_first")
        self.assertEqual(ls_first["description"], "ls first")
        self.assertEqual(ls_first["prompt"], "always call ls")

    def test_memory_category_empty_when_no_lessons(self):
        result = ralph.fulfill_peer_request("recipes", {"from": "x"})
        self.assertEqual(result, {"lessons": []})

    def test_unknown_category_returns_error(self):
        result = ralph.fulfill_peer_request("question", {"from": "x"})
        self.assertIn("error", result)
        self.assertIn("unknown category", result["error"])


class AskRalphValidationTests(unittest.TestCase):
    """The ask_ralph builtin rejects categories outside the allowed set before hitting the bus."""

    def test_rejects_unknown_category(self):
        with patch.object(config, "bus", MagicMock()) as mock_bus:
            result = json.loads(tools_mod.handle_tool(
                "ask_ralph", {"id": "beta", "category": "anything"}, {},
            ))
            self.assertIn("error", result)
            self.assertIn("category must be one of", result["error"])
            mock_bus.ask.assert_not_called()

    def test_passes_valid_category_to_bus(self):
        mock_bus = MagicMock()
        mock_bus.ask.return_value = {"request_id": "abc", "status": "sent"}
        with patch.object(config, "bus", mock_bus):
            result = json.loads(tools_mod.handle_tool(
                "ask_ralph", {"id": "beta", "category": "tools"}, {},
            ))
        self.assertEqual(result, {"request_id": "abc", "status": "sent"})
        mock_bus.ask.assert_called_once_with("beta", "tools")


if __name__ == "__main__":
    unittest.main()
