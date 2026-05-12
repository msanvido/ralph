"""Memory: extract durable recipes after each iteration; surface relevant ones before the next.

Recipes are procedural/heuristic knowledge for problems that don't reduce cleanly to a single
Python tool — multi-step strategies, when-to-use-which-approach, hard-won judgment calls.
Anything algorithmic should be codified as a TOOL in workspace/tools/ instead.

Storage: a single JSON file at workspace/memory/memory.json, shaped like

    {
      "recipes": {
        "<lesson_id>": {
          "description": "short — used for selection",
          "prompt":      "full recipe — loaded into context when selected",
          "ts":          1716495200.0
        },
        ...
      }
    }

Categories are top-level keys. `ts` is the LRU timestamp (touched on add and on select);
when the total lesson count exceeds MEMORY_LRU_LIMIT, the oldest lessons are evicted on add.
"""
import json
import re
import time
from pathlib import Path

from . import config
from .tools import to_openai_tool

# Single-element tuple kept for forward compatibility with code that iterates categories
# (load_index, fulfill_peer_request, etc.). If we ever want a finer split again, this is
# the only place to add the new buckets.
MEMORY_CATEGORIES = ("recipes",)
MEMORY_LRU_LIMIT = 100  # max total lessons across all categories


# === Store: single JSON file holding all memory ===

def _store_path() -> Path:
    return config.MEMORY_DIR / "memory.json"


def _empty_store() -> dict:
    return {cat: {} for cat in MEMORY_CATEGORIES}


def load_store() -> dict:
    """Read the entire memory store. Always returns a dict with every category key present."""
    path = _store_path()
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return _empty_store()
    store = _empty_store()
    if isinstance(data, dict):
        for cat in MEMORY_CATEGORIES:
            bucket = data.get(cat)
            if isinstance(bucket, dict):
                store[cat] = {k: v for k, v in bucket.items() if isinstance(v, dict)}
    return store


def save_store(store: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=True))


def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s[:max_len] or "lesson").rstrip("_")


# === Public read API ===

def load_index(category: str) -> list[dict]:
    """Return [{"id", "description"}, ...] for a category, or []."""
    bucket = load_store().get(category) or {}
    return [{"id": lid, "description": entry.get("description", "")}
            for lid, entry in bucket.items()]


def load_lesson(category: str, lesson_id: str) -> dict | None:
    """Return {"description", "prompt"} for one lesson, or None."""
    entry = (load_store().get(category) or {}).get(lesson_id)
    if not isinstance(entry, dict):
        return None
    return {
        "description": entry.get("description", ""),
        "prompt": entry.get("prompt", ""),
    }


def read_memory_items() -> list[tuple[str, str, str]]:
    """Return (catalog_id, category, description) for every lesson across all categories.
    catalog_id is "<category>:<slug>" — stable, descriptive."""
    items: list[tuple[str, str, str]] = []
    store = load_store()
    for cat in MEMORY_CATEGORIES:
        for lid, entry in (store.get(cat) or {}).items():
            description = entry.get("description")
            if description:
                items.append((f"{cat}:{lid}", cat, description))
    return items


# === Public write API ===

def add_lesson(category: str, description: str, prompt: str) -> str | None:
    """Append a lesson to the store. Returns the new lesson id, or None on invalid input.
    Stamps the lesson's LRU timestamp and evicts oldest lessons if over the cap."""
    if category not in MEMORY_CATEGORIES:
        return None
    description = description.strip()
    prompt = prompt.strip()
    if not description or not prompt:
        return None
    store = load_store()
    bucket = store.setdefault(category, {})
    base = _slugify(description)
    lesson_id = base
    n = 2
    while lesson_id in bucket:
        lesson_id = f"{base}_{n}"
        n += 1
    bucket[lesson_id] = {
        "description": description,
        "prompt": prompt,
        "ts": time.time(),
    }
    _evict_if_needed(store)
    save_store(store)
    return lesson_id


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


# === LRU eviction (cap total lessons at MEMORY_LRU_LIMIT) ===

def _evict_if_needed(store: dict) -> list[str]:
    """If total lesson count > MEMORY_LRU_LIMIT, drop the oldest in place. Returns catalog_ids."""
    flat = [(cat, lid, entry.get("ts", 0.0))
            for cat in MEMORY_CATEGORIES
            for lid, entry in (store.get(cat) or {}).items()]
    if len(flat) <= MEMORY_LRU_LIMIT:
        return []
    flat.sort(key=lambda x: x[2])
    excess = len(flat) - MEMORY_LRU_LIMIT
    evicted: list[str] = []
    for cat, lid, _ts in flat[:excess]:
        store[cat].pop(lid, None)
        evicted.append(f"{cat}:{lid}")
    if evicted:
        print(f"  🗑️  evicted {len(evicted)} memory lesson(s) (LRU)")
    return evicted


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
    store = load_store()
    items: list[tuple[str, str, str]] = []
    for cat in MEMORY_CATEGORIES:
        for lid, entry in (store.get(cat) or {}).items():
            description = entry.get("description")
            if description:
                items.append((f"{cat}:{lid}", cat, description))
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
    touched = False
    now = time.time()
    for cid in selected:
        if ":" not in cid:
            continue
        cat, lid = cid.split(":", 1)
        if cat not in MEMORY_CATEGORIES:
            continue
        entry = (store.get(cat) or {}).get(lid)
        if entry and entry.get("prompt"):
            entry["ts"] = now
            touched = True
            recipes.append(f"- {entry['prompt']}")
    if touched:
        save_store(store)
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
