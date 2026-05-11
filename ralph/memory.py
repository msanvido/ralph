"""Memory: extract durable recipes after each iteration; surface relevant ones before the next.

Recipes are procedural/heuristic knowledge for problems that don't reduce cleanly to a single
Python tool — multi-step strategies, when-to-use-which-approach, hard-won judgment calls.
Anything algorithmic should be codified as a TOOL in workspace/tools/ instead.

Storage layout (mirrors workspace/tools/ — one file per item):

  workspace/memory/
    recipes/
      _index.py          INDEX = [{"id": "...", "description": "..."}, ...]
      <slug>.py          MEMORY = {"description": "...", "prompt": "..."}

`description` is short — used for selection / discovery.
`prompt` is the full recipe — loaded into context only when selected.
"""
import json
import re
from pathlib import Path

from . import config
from .tools import LRUStore, load_py_module, to_openai_tool, write_py_literal

# Single-element tuple kept for forward compatibility with code that iterates categories
# (load_index, fulfill_peer_request, etc.). If we ever want a finer split again, this is
# the only place to add the new buckets.
MEMORY_CATEGORIES = ("recipes",)
MEMORY_LRU_LIMIT = 100  # max total lessons across all categories


# === Slug + file helpers ===

def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s[:max_len] or "lesson").rstrip("_")


def _lesson_source(description: str, prompt: str) -> str:
    return (
        "MEMORY = {\n"
        f"    \"description\": {json.dumps(description)},\n"
        f"    \"prompt\": {json.dumps(prompt)},\n"
        "}\n"
    )


def _write_index(path: Path, index: list[dict]) -> None:
    write_py_literal(path, "INDEX", index)


def load_index(category: str) -> list[dict]:
    """Read the per-category index. Returns [{"id", "description"}, ...] or []."""
    path = config.MEMORY_DIR / category / "_index.py"
    if not path.exists():
        return []
    module = load_py_module(f"_ralph_index_{category}", path)
    if module is None:
        return []
    index = getattr(module, "INDEX", None)
    return index if isinstance(index, list) else []


def load_lesson(category: str, lesson_id: str) -> dict | None:
    """Read a single lesson file. Returns {"description", "prompt"} or None."""
    path = config.MEMORY_DIR / category / f"{lesson_id}.py"
    if not path.exists():
        return None
    module = load_py_module(f"_ralph_memory_{category}_{lesson_id}", path)
    if module is None:
        return None
    mem = getattr(module, "MEMORY", None)
    if not isinstance(mem, dict):
        return None
    return mem


def add_lesson(category: str, description: str, prompt: str) -> str | None:
    """Write a new lesson file and update the category index. Returns the slug, or None.
    Also bumps the lesson's LRU timestamp and evicts oldest lessons if over the cap."""
    if category not in MEMORY_CATEGORIES:
        return None
    description = description.strip()
    prompt = prompt.strip()
    if not description or not prompt:
        return None
    cat_dir = config.MEMORY_DIR / category
    cat_dir.mkdir(parents=True, exist_ok=True)

    base = _slugify(description)
    lesson_id = base
    n = 2
    while (cat_dir / f"{lesson_id}.py").exists():
        lesson_id = f"{base}_{n}"
        n += 1

    (cat_dir / f"{lesson_id}.py").write_text(_lesson_source(description, prompt))

    index = load_index(category)
    index.append({"id": lesson_id, "description": description})
    _write_index(cat_dir / "_index.py", index)

    _touch_memory(f"{category}:{lesson_id}")
    _evict_memory_if_needed()
    return lesson_id


# === LRU eviction (cap total lessons at MEMORY_LRU_LIMIT) ===

_MEMORY_LRU = LRUStore(lambda: config.MEMORY_DIR / "_lru.py")


def _touch_memory(catalog_id: str) -> None:
    _MEMORY_LRU.touch(catalog_id)


def _delete_lesson(catalog_id: str) -> None:
    cat, lesson_id = catalog_id.split(":", 1)
    cat_dir = config.MEMORY_DIR / cat
    (cat_dir / f"{lesson_id}.py").unlink(missing_ok=True)
    index = [e for e in load_index(cat) if e.get("id") != lesson_id]
    _write_index(cat_dir / "_index.py", index)


def _evict_memory_if_needed() -> list[str]:
    """If total lesson count > MEMORY_LRU_LIMIT, evict oldest. Returns evicted catalog_ids."""
    items = read_memory_items()
    if len(items) <= MEMORY_LRU_LIMIT:
        return []
    lru = _MEMORY_LRU.load()
    pairs = sorted(((cid, lru.get(cid, 0.0)) for cid, _, _ in items), key=lambda x: x[1])
    excess = len(items) - MEMORY_LRU_LIMIT
    evicted: list[str] = []
    for cid, _ts in pairs[:excess]:
        _delete_lesson(cid)
        lru.pop(cid, None)
        evicted.append(cid)
    if evicted:
        _MEMORY_LRU.save(lru)
        print(f"  🗑️  evicted {len(evicted)} memory lesson(s) (LRU)")
    return evicted


# === Catalog (used by select_relevant_memories) ===

def read_memory_items() -> list[tuple[str, str, str]]:
    """Return (catalog_id, category, description) for every lesson across all categories.
    catalog_id is "<category>:<slug>" — stable, descriptive."""
    items: list[tuple[str, str, str]] = []
    for cat in MEMORY_CATEGORIES:
        for entry in load_index(cat):
            lesson_id = entry.get("id")
            description = entry.get("description")
            if lesson_id and description:
                items.append((f"{cat}:{lesson_id}", cat, description))
    return items


def append_memory(learnings: dict) -> int:
    """Persist new lessons. Each category contains a list of {description, prompt} dicts."""
    total = 0
    for cat in MEMORY_CATEGORIES:
        for entry in (learnings.get(cat) or []):
            if not isinstance(entry, dict):
                continue
            if add_lesson(cat, entry.get("description", ""), entry.get("prompt", "")):
                total += 1
    return total


# === Transcript formatting ===

def format_transcript(messages: list[dict]) -> str:
    lines: list[str] = []
    for m in messages:
        role = m["role"].upper()
        content = m.get("content")
        if role == "TOOL":
            text = str(content or "")
            if len(text) > 200:
                text = text[:197] + "..."
            lines.append(f"  TOOL_RESULT[{m.get('tool_call_id')}] {text}")
            continue
        if content:
            lines.append(f"{role}: {str(content).strip()}")
        for tc in m.get("tool_calls") or []:
            fn = tc["function"]
            args = fn.get("arguments") or "{}"
            if len(args) > 200:
                args = args[:197] + "..."
            lines.append(f"  TOOL_USE {fn.get('name')}({args})")
    return "\n".join(lines)


# === Forced-tool helper ===

def _call_forced_tool(
    *, system: str, user: str, tool: dict, tool_name: str, max_tokens: int, label: str,
) -> dict | None:
    try:
        response = config.completion(
            model=config.MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
    except Exception as e:
        print(f"  ⚠️  {label} failed: {type(e).__name__}: {e}")
        return None
    msg = response.choices[0].message
    for tc in (msg.tool_calls or []):
        if tc.function.name == tool_name:
            try:
                return json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                return None
    return None


# === Expertise extraction ===

EXPERTISE_SYSTEM = """You are summarizing what a Ralph's task expects them to specialize in.
Read the prompt and return a single short phrase (3-8 words) capturing the expertise area —
the kind of work this Ralph will become good at and that other Ralphs might ask them for help on.

Examples:
  prompt about solving sudoku → "Sudoku solving"
  prompt about scraping product pages → "Web scraping"
  prompt about a watcher that summarizes peers → "Multi-agent observability"

Skip filler words. No quotes. Call set_expertise exactly once."""

EXPERTISE_TOOL = to_openai_tool({
    "name": "set_expertise",
    "description": "Set this Ralph's expertise as one short phrase.",
    "input_schema": {
        "type": "object",
        "properties": {"phrase": {"type": "string"}},
        "required": ["phrase"],
    },
})


def extract_expertise(prompt: str) -> str:
    """One-shot LLM call to summarize this Ralph's expertise from its prompt. Returns ""
    on failure — callers should treat the empty string as 'no expertise published'."""
    args = _call_forced_tool(
        system=EXPERTISE_SYSTEM,
        user=f"Prompt:\n{prompt}",
        tool=EXPERTISE_TOOL,
        tool_name="set_expertise",
        max_tokens=64,
        label="expertise extraction",
    ) or {}
    phrase = (args.get("phrase") or "").strip()
    return phrase


# === Selection ===

SELECT_SYSTEM = """You pick which past recipes are relevant to the task you're about to attempt.
You'll see a numbered catalog of recipes — reusable procedural/heuristic knowledge from
past iterations. Return ONLY the IDs of recipes that would actually help on THIS specific
task. Skip generic or off-topic ones. Better to skip than to load noise. If nothing applies,
pass an empty list. Call select_memories exactly once."""

SELECT_TOOL = to_openai_tool({
    "name": "select_memories",
    "description": "Pick the IDs of memories that are relevant to the upcoming task.",
    "input_schema": {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["ids"],
    },
})


def select_relevant_memories(prompt: str) -> str:
    items = read_memory_items()
    if not items:
        return ""
    catalog = "\n".join(f"[{cid}] ({cat}) {desc}" for cid, cat, desc in items)
    args = _call_forced_tool(
        system=SELECT_SYSTEM,
        user=f"Task:\n{prompt}\n\nMemory catalog:\n{catalog}",
        tool=SELECT_TOOL,
        tool_name="select_memories",
        max_tokens=512,
        label="memory selection",
    ) or {}
    selected = {s for s in (args.get("ids") or []) if isinstance(s, str)}
    if not selected:
        return ""
    recipes: list[str] = []
    for cid, cat, _desc in items:
        if cid not in selected:
            continue
        _, lesson_id = cid.split(":", 1)
        lesson = load_lesson(cat, lesson_id)
        if lesson and lesson.get("prompt"):
            _touch_memory(cid)
            recipes.append(f"- {lesson['prompt']}")
    if not recipes:
        return ""
    print(f"  📚 recalled {len(recipes)} relevant recipe(s)")
    return (
        "## Recipes from past iterations\n\n"
        + "\n".join(recipes)
        + "\n\n---\n\n"
    )


# === Learn ===

LEARN_SYSTEM = """You are Ralph reviewing the iteration you just completed. Extract durable
RECIPES — procedural/heuristic knowledge — that will help in future iterations on similar tasks.

A recipe captures something that's hard to solve algorithmically: a strategy, a decision
heuristic, a when-to-use-which-approach, hard-won judgment. Anything that boils down to a
clean Python routine should be CODE (in workspace/tools/), not a recipe. Don't write recipes
for trivially algorithmic things; don't write recipes that just restate the task.

Each recipe has TWO fields:
- description: one short line — used to decide whether the recipe is relevant to a future task.
- prompt: the full recipe — steps, when to apply, what to watch out for, with examples —
  loaded verbatim into a future iteration's context when selected.

Skip if nothing this iteration is worth distilling (pass an empty list). Call
record_learnings exactly once."""

LEARN_TOOL = to_openai_tool({
    "name": "record_learnings",
    "description": "Record durable recipes. Each recipe has a short description (for selection) and a longer prompt (loaded into context when selected).",
    "input_schema": {
        "type": "object",
        "properties": {
            cat: {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                    "required": ["description", "prompt"],
                },
            } for cat in MEMORY_CATEGORIES
        },
        "required": list(MEMORY_CATEGORIES),
    },
})


def learn_from_iteration(messages: list[dict], n: int) -> None:
    if len(messages) <= 1:
        return
    args = _call_forced_tool(
        system=LEARN_SYSTEM,
        user=f"Iteration {n} transcript:\n\n{format_transcript(messages)}",
        tool=LEARN_TOOL,
        tool_name="record_learnings",
        max_tokens=2048,
        label="learn phase",
    )
    if args is None:
        return
    count = append_memory(args)
    if count:
        print(f"  📚 learned {count} new recipe(s)")
