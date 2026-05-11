"""All tool definitions: builtin schemas + handlers, dynamic loader, dispatch."""
import importlib.util
import json
import re
import time
import traceback
from pathlib import Path

from . import config

TOOLS_LRU_LIMIT = 100  # max number of dynamic tool files retained in workspace/tools/


# === Shared helpers (also used by memory.py / ralph.py) ===

def load_py_module(unique_name: str, path: Path):
    """Import a .py file as a module. Returns the module, or None on import error."""
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def write_py_literal(path: Path, name: str, value, sort_keys: bool = False) -> None:
    """Write a JSON-serializable value as a top-level Python assignment (`NAME = ...`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{name} = " + json.dumps(value, indent=4, sort_keys=sort_keys) + "\n")


class LRUStore:
    """Tiny LRU bookkeeper persisted as a `LRU = {...}` Python literal."""

    def __init__(self, path_fn):
        # path_fn() lets the caller defer resolution until config has been mutated.
        self._path_fn = path_fn

    def load(self) -> dict[str, float]:
        path = self._path_fn()
        if not path.exists():
            return {}
        module = load_py_module(f"_ralph_lru_{path.parent.name}", path)
        if module is None:
            return {}
        return dict(getattr(module, "LRU", {}) or {})

    def save(self, lru: dict) -> None:
        write_py_literal(self._path_fn(), "LRU", lru, sort_keys=True)

    def touch(self, key: str) -> None:
        lru = self.load()
        lru[key] = time.time()
        self.save(lru)


def user_tool_files() -> list[Path]:
    """Sorted .py files in TOOLS_DIR that are user-defined (not _-prefixed)."""
    if not config.TOOLS_DIR.is_dir():
        return []
    return [p for p in sorted(config.TOOLS_DIR.glob("*.py")) if not p.name.startswith("_")]


def iter_user_tools():
    """Yield (path, tool_def) for each user tool with a parseable TOOL = {...} dict."""
    for p in user_tool_files():
        module = load_py_module(f"_peer_tool_{p.stem}", p)
        if module is None:
            continue
        tool_def = getattr(module, "TOOL", None)
        if isinstance(tool_def, dict):
            yield p, tool_def


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def score_keyword_overlap(query: str, text: str) -> int:
    """Count distinct alphanumeric tokens (length >= 3) shared between query and text.
    Used by peer-ask fulfillment to rank tools/lessons by relevance to a request description."""
    def toks(s: str) -> set[str]:
        return {t for t in _TOKEN_RE.findall(s.lower()) if len(t) >= 3}
    return len(toks(query) & toks(text))


# === Path helpers ===

def safe_path(rel: str) -> Path:
    root = config.WORKSPACE.resolve()
    p = (config.WORKSPACE / rel).resolve()
    if root != p and root not in p.parents:
        raise ValueError(f"path escapes workspace: {rel}")
    return p


def _rel(p: Path) -> str:
    return str(p.relative_to(config.WORKSPACE.resolve()))


# === Filesystem builtins ===

def _ls(path: str = ".", recursive: bool = False) -> dict:
    base = safe_path(path)
    if base.is_file():
        return {"paths": [_rel(base)]}
    if not base.is_dir():
        return {"paths": []}
    if recursive:
        paths = sorted(_rel(p) for p in base.rglob("*") if p.is_file())
    else:
        paths = sorted(_rel(p) + ("/" if p.is_dir() else "") for p in base.iterdir())
    return {"paths": paths}


def _read(path: str) -> dict:
    return {"content": safe_path(path).read_text()}


def _write(path: str, content: str) -> dict:
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return {"status": "written", "bytes": len(content)}


def _edit(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
    if old_string == new_string:
        raise ValueError("old_string and new_string must differ")
    p = safe_path(path)
    content = p.read_text()
    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"old_string not found in {path}")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string appears {count} times in {path} — pass replace_all=true or include more surrounding context"
        )
    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    p.write_text(new_content)
    return {"status": "edited", "replacements": count if replace_all else 1}


def _grep(pattern: str, path: str = ".", glob: str | None = None) -> dict:
    rx = re.compile(pattern)
    base = safe_path(path)
    if base.is_file():
        candidates = [base]
    elif base.is_dir():
        candidates = list(base.rglob(glob)) if glob else [p for p in base.rglob("*") if p.is_file()]
    else:
        candidates = []
    matches = []
    for p in candidates:
        if not p.is_file():
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                matches.append({"file": _rel(p), "line": i, "text": line.rstrip()})
    return {"matches": matches, "count": len(matches)}


def _find(name: str, path: str = ".") -> dict:
    base = safe_path(path)
    if not base.is_dir():
        return {"paths": []}
    return {"paths": sorted(_rel(p) for p in base.rglob(name) if p.is_file())}


def _mark_done() -> dict:
    config.DONE_MARKER.write_text("done")
    return {"status": "done"}


# === Bus builtins (require config.bus to be initialized) ===

def _require_bus():
    if config.bus is None:
        raise RuntimeError("bus is not initialized — pass --bus-dir at startup")
    return config.bus


def _list_ralphs() -> dict:
    return {"ralphs": _require_bus().list_ralphs()}


def _peek_ralph(id: str) -> dict:
    return _require_bus().peek_ralph(id)


PEER_SHARE_CATEGORIES = ("tools", "recipes")


def _ask_ralph(id: str, category: str, description: str) -> dict:
    if category not in PEER_SHARE_CATEGORIES:
        raise ValueError(f"category must be one of {list(PEER_SHARE_CATEGORIES)}; got {category!r}")
    description = (description or "").strip()
    if not description:
        raise ValueError("description must be a non-empty string describing what you need")
    return _require_bus().ask(id, category, description)


def _check_response(request_id: str) -> dict:
    """Return the peer's response, but if it's a 'tools' answer (shape {files: {...}}),
    write each file directly to workspace/tools/ and return a summary instead of the raw
    source. This keeps the source bytes verbatim — the LLM never retypes them — and saves
    the requester from a redundant `write` round-trip."""
    response = _require_bus().check_response(request_id)
    if response.get("status") == "pending":
        return response
    answer = response.get("answer")
    if isinstance(answer, dict) and isinstance(answer.get("files"), dict):
        config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        installed: list[dict] = []
        for filename, source in answer["files"].items():
            if not isinstance(filename, str) or not isinstance(source, str):
                continue
            # Path(...).name strips any path components a careless or hostile peer might
            # include, so an "../etc/passwd.py" can never escape TOOLS_DIR.
            name = Path(filename).name
            if not name.endswith(".py") or name.startswith("_"):
                continue
            (config.TOOLS_DIR / name).write_text(source)
            installed.append({"filename": name, "bytes": len(source.encode())})
        if installed:
            note = (
                "Tools were auto-installed to workspace/tools/ verbatim. The loop "
                "reloads tools after any change, so they're callable on your very "
                "next tool_use — no `write` needed."
            )
        else:
            # Honest message: the peer returned an EMPTY files dict. Re-polling the same
            # request_id will keep returning the same empty answer, so the agent should
            # do something different next turn.
            note = (
                "Peer returned an empty `files` map — they probably haven't built a "
                "matching tool yet, or your `description` didn't match anything they "
                "have. Do NOT keep calling check_response with this same request_id "
                "(the answer won't change). Either wait an iteration and ask again "
                "with a fresh ask_ralph, try a different peer, or revise the description."
            )
        return {
            "request_id": request_id,
            "from": response.get("from"),
            "installed_tools": installed,
            "note": note,
        }
    return response


# === Builtin schemas ===

BUILTIN_TOOLS = [
    {
        "name": "ls",
        "description": "List directory contents inside workspace/. Default: workspace root, one level. Pass recursive=true to walk all files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace/. Default: '.'"},
                "recursive": {"type": "boolean", "description": "Walk recursively. Default: false."},
            },
        },
    },
    {
        "name": "read",
        "description": "Read a file from workspace/. Path is relative to workspace/.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write",
        "description": "Write content to a file. Path is relative to workspace/. Creates parent dirs as needed. Overwrites existing files. Prefer `edit` for surgical changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit",
        "description": "Replace old_string with new_string in a file. Errors if old_string is missing or appears multiple times (unless replace_all=true).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default: false."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "grep",
        "description": "Search files in workspace/ for a Python regex pattern. Returns matching lines with file path and line number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex."},
                "path": {"type": "string", "description": "Search root, relative to workspace/. Default: '.'"},
                "glob": {"type": "string", "description": "Optional filename glob like '*.py' to filter files."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "find",
        "description": "Find files in workspace/ matching a filename glob like '*.py' or 'config.*'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename glob pattern."},
                "path": {"type": "string", "description": "Search root, relative to workspace/. Default: '.'"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "mark_done",
        "description": "Signal the task is fully complete and the loop should stop. Only call when every part of the task is finished.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_ralphs",
        "description": "List the IDs of all Ralphs currently visible on the bus.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "peek_ralph",
        "description": "Read another Ralph's most recent status (iteration, last_text, done).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "ask_ralph",
        "description": (
            "Ask another Ralph to share what they learned in a category. Include a "
            "`description` of what you're trying to do — the peer ranks their artifacts "
            "against it and sends back the top 3 most relevant. Returns immediately with a "
            "request_id; poll check_response on later turns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Target Ralph's id."},
                "category": {
                    "type": "string",
                    "enum": list(PEER_SHARE_CATEGORIES),
                    "description": (
                        "What to fetch: 'tools' (the peer's dynamic tool source files) "
                        "or 'recipes' (the peer's procedural/heuristic knowledge)."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What you need help with — a short phrase or sentence "
                        "(e.g., 'solve a sudoku puzzle', 'parse CSV with quoted commas'). "
                        "The peer uses this to pick the best tool/lesson to send back."
                    ),
                },
            },
            "required": ["id", "category", "description"],
        },
    },
    {
        "name": "check_response",
        "description": (
            "Check whether your ask_ralph has been answered. Returns {status:'pending'} "
            "until the answer arrives. When the answer is a 'tools' bundle, the files "
            "are AUTO-INSTALLED into your tools/ directory verbatim (no manual `write` "
            "needed) and the response shows {installed_tools: [...]}; the loop reloads "
            "tools so they're callable on your very next tool_use. The 'recipes' "
            "category returns {answer: {lessons: [...]}} unchanged for you to read and apply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
        },
    },
]


BUILTIN_HANDLERS = {
    "ls": _ls,
    "read": _read,
    "write": _write,
    "edit": _edit,
    "grep": _grep,
    "find": _find,
    "mark_done": _mark_done,
    "list_ralphs": _list_ralphs,
    "peek_ralph": _peek_ralph,
    "ask_ralph": _ask_ralph,
    "check_response": _check_response,
}
BUILTIN_NAMES = frozenset(BUILTIN_HANDLERS)


def to_openai_tool(tool: dict) -> dict:
    """Normalize a tool schema to OpenAI 'function' format.

    Accepts both shapes (idempotent on OpenAI):
      Anthropic: {name, description, input_schema}
      OpenAI:    {type: "function", function: {name, description, parameters}}
    """
    if isinstance(tool, dict) and tool.get("type") == "function" and "function" in tool:
        return tool
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


# Pre-normalized once at module load — builtins don't change at runtime.
BUILTIN_TOOLS_OPENAI = [to_openai_tool(t) for t in BUILTIN_TOOLS]


# === LRU eviction (cap dynamic tools at TOOLS_LRU_LIMIT) ===

_TOOLS_LRU = LRUStore(lambda: config.TOOLS_DIR / "_lru.py")


def _touch_tool(filename: str) -> None:
    _TOOLS_LRU.touch(filename)


def _evict_tools_if_needed() -> list[str]:
    """If over TOOLS_LRU_LIMIT, delete the least-recently-used tool files. Returns evicted filenames."""
    files = user_tool_files()
    if len(files) <= TOOLS_LRU_LIMIT:
        return []
    lru = _TOOLS_LRU.load()
    pairs = sorted(((p.name, lru.get(p.name, 0.0)) for p in files), key=lambda x: x[1])
    excess = len(files) - TOOLS_LRU_LIMIT
    evicted: list[str] = []
    for name, _ts in pairs[:excess]:
        (config.TOOLS_DIR / name).unlink(missing_ok=True)
        lru.pop(name, None)
        evicted.append(name)
    if evicted:
        _TOOLS_LRU.save(lru)
    return evicted


# === Dynamic tool loader ===

def load_dynamic_tools() -> tuple[list[dict], dict, list[str]]:
    """Load every TOOLS_DIR/*.py as a tool. Returns (schemas, dispatch, warnings).
    Each handler is wrapped to bump the tool's LRU timestamp on call."""
    evicted = _evict_tools_if_needed()
    schemas: list[dict] = []
    dispatch: dict = {}
    warnings: list[str] = [f"evicted (LRU): {f}" for f in evicted]
    for path in user_tool_files():
        module = load_py_module(f"ralph_tools.{path.stem}", path)
        if module is None:
            warnings.append(f"load {path.name} failed")
            continue
        tool = getattr(module, "TOOL", None)
        run = getattr(module, "run", None)
        if not isinstance(tool, dict) or not callable(run):
            warnings.append(f"{path.name} missing TOOL dict or run() — skipping")
            continue
        name = tool.get("name")
        if not name or name in BUILTIN_NAMES or name in dispatch:
            warnings.append(f"{path.name} has invalid/duplicate name {name!r} — skipping")
            continue
        schemas.append(tool)
        dispatch[name] = _wrap_with_lru_touch(run, path.name)
    return schemas, dispatch, warnings


def _wrap_with_lru_touch(run_fn, filename: str):
    def handler(**kwargs):
        _touch_tool(filename)
        return run_fn(**kwargs)
    return handler


# === Dispatch ===

def handle_tool(name: str, input: dict, dynamic_dispatch: dict) -> str:
    handler = BUILTIN_HANDLERS.get(name) or dynamic_dispatch.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        result = handler(**input)
    except Exception:
        return json.dumps({"error": traceback.format_exc(limit=3)})
    return result if isinstance(result, str) else json.dumps(result)
