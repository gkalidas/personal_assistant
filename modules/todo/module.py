"""Todo module — natural-language access to the dashboard's todo store.

The todo list already exists as a dashboard feature (SQLite, served via
/api/todos and the /todo page), but the chat router had no module to send
todo phrasings to, so "show todo list" misrouted to system/finance/general.
This module closes that gap.

Routing is deterministic keyword classification (no LLM) — todos are simple
CRUD, so an LLM round-trip would only add latency and failure modes. Mirrors
the diary module's intent-by-keyword approach.

Intents:
  "show todo list" / "what are my tasks"   → list pending todos
  "add todo <text>" / "remind me to <text>" → add a todo
  "mark <text> done" / "complete <text>"    → mark a todo done (runs verifier)
  "delete todo <text>"                      → delete a todo
"""

import logging
import re
from typing import Any

from core.base_module import BaseModule, ModuleResponse
from dashboard.todo_store import (
    ensure_table, list_todos, add_todo, update_todo, delete_todo,
)

log = logging.getLogger(__name__)

# Eisenhower quadrant → human label, for display.
_QUAD_LABEL = {
    "q1": "Urgent & Important",
    "q2": "Important",
    "q3": "Urgent",
    "q4": "Later",
}

# Verifier state (todos.db `verified` column) → glyph.
_VERIFY_GLYPH = {1: "✓", -1: "✗", 0: ""}

# Words that introduce a new todo's text — stripped off by _strip_add_prefix.
_ADD_WORDS = ("add todo", "add task", "new todo", "new task", "create todo",
              "create task", "remind me to", "add to my list", "add to todo",
              "add a todo", "add a task")

# Intent detection is verb-anchored (regex) rather than fixed bigrams, so that
# words can sit between the verb and "todo" (e.g. "delete the searxng todo",
# "mark the pump task done"). Checked in order: add → delete → done → list.
_ADD_RE    = re.compile(r"\bremind me to\b|\b(add|create|new)\b.*\b(todo|task|list)\b"
                        r"|\b(add|create|new)\b.*\bto (?:my )?list\b")
_DELETE_RE = re.compile(r"\b(delete|remove|drop)\b")
_DONE_RE   = re.compile(r"\b(done|complete[d]?|finish(?:ed)?)\b|\b(tick|check) off\b|^mark\b")

_ID_RE = re.compile(r"\b([0-9a-f]{8})\b")


def _intent(query: str) -> str:
    """Classify a todo query into add / done / delete / list (no LLM)."""
    q = query.lower().strip()
    if _ADD_RE.search(q):
        return "add"
    if _DELETE_RE.search(q):
        return "delete"
    if _DONE_RE.search(q):
        return "done"
    return "list"


def _detect_quad(text: str) -> str:
    """Pick an Eisenhower quadrant from words like 'urgent'/'important'. Default q2."""
    t = text.lower()
    urgent = "urgent" in t or "asap" in t or "today" in t
    important = "important" in t
    if urgent and important:
        return "q1"
    if urgent:
        return "q3"
    return "q2"  # default: important-but-not-urgent


def _strip_add_prefix(query: str) -> str:
    """Remove the leading 'add todo'/'remind me to'/… phrase, return the task text."""
    q = query.strip()
    low = q.lower()
    for w in sorted(_ADD_WORDS, key=len, reverse=True):
        if low.startswith(w):
            return q[len(w):].strip(" :,-")
        # also handle 'please add todo …'
        idx = low.find(w)
        if idx != -1:
            return q[idx + len(w):].strip(" :,-")
    return q


def _find_todo(query: str, todos: list[dict]) -> tuple[dict | None, str | None]:
    """Locate the todo a done/delete query refers to.

    Match priority: explicit 8-char id, then best word-overlap on the text.
    Returns (todo, error_message). Exactly one of the two is non-None.
    """
    if not todos:
        return None, "Your todo list is empty."

    m = _ID_RE.search(query)
    if m:
        for t in todos:
            if t["id"] == m.group(1):
                return t, None
        return None, f"No todo with id {m.group(1)}."

    # Word-overlap match: drop intent verbs/stopwords, score the rest against each todo.
    _STOP = {"todo", "todos", "task", "tasks", "the", "my", "mark", "done", "delete",
             "remove", "drop", "complete", "completed", "finish", "finished", "off",
             "tick", "check", "as", "list", "from"}
    terms = [w for w in re.findall(r"[a-z0-9]+", query.lower())
             if len(w) > 2 and w not in _STOP]
    if not terms:
        return None, "Tell me which todo — include a word or two from it, or its id."

    scored = []
    for t in todos:
        text_l = t["text"].lower()
        score = sum(1 for term in terms if term in text_l)
        if score:
            scored.append((score, t))
    if not scored:
        return None, "No todo matched that. Try 'show todo list' to see ids."
    scored.sort(key=lambda s: s[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        names = ", ".join(f"{t['id']} ({t['text'][:30]})" for _, t in scored[:3])
        return None, f"Ambiguous — which one? {names}"
    return scored[0][1], None


# ── Intent handlers ───────────────────────────────────────────────────────────

def _do_list() -> tuple[str, dict | None]:
    """Show pending todos grouped by Eisenhower quadrant."""
    todos = list_todos(include_done=False)
    if not todos:
        return ("Your todo list is empty. Say \"add todo <something>\" to start one.",
                {"count": 0})

    by_quad: dict[str, list[dict]] = {}
    for t in todos:
        by_quad.setdefault(t.get("quad", "q2"), []).append(t)

    lines = [f"Todo list — {len(todos)} pending:"]
    for quad in ("q1", "q2", "q3", "q4"):
        items = by_quad.get(quad)
        if not items:
            continue
        lines.append(f"\n{_QUAD_LABEL[quad]}:")
        for t in items:
            glyph = _VERIFY_GLYPH.get(t.get("verified", 0), "")
            tag = f" {glyph}" if glyph else ""
            lines.append(f"  ○ {t['text']}{tag}  [{t['id']}]")
    return "\n".join(lines), {"count": len(todos)}


def _do_add(query: str) -> tuple[str, dict | None]:
    """Add a new todo parsed from the query."""
    text = _strip_add_prefix(query)
    if not text:
        return "What should I add to your todo list?", None
    quad = _detect_quad(query)
    tid = add_todo(text, quad)
    log.info("todo added id=%s quad=%s text=%r", tid, quad, text[:60])
    return f"Added to {_QUAD_LABEL[quad]}: {text}  [{tid}]", {"id": tid, "quad": quad}


def _do_done(query: str) -> tuple[str, dict | None]:
    """Mark a todo done. Runs its verifier first if one is attached."""
    todos = list_todos(include_done=False)
    todo, err = _find_todo(query, todos)
    if err:
        return err, None

    if todo.get("verifier"):
        try:
            from dashboard.todo_verifier import run_verifier
            passed, note = run_verifier(todo["verifier"])
        except Exception as e:
            log.warning("verifier run failed for %s: %s", todo["id"], e)
            passed, note = False, f"verifier error: {e}"
        if not passed:
            update_todo(todo["id"], verified=-1, verify_note=note)
            return (f"Can't mark \"{todo['text']}\" done yet — verification failed: {note}",
                    {"id": todo["id"], "blocked": True})
        update_todo(todo["id"], verified=1, verify_note=note)

    update_todo(todo["id"], done=1)
    log.info("todo done id=%s text=%r", todo["id"], todo["text"][:60])
    return f"Marked done: {todo['text']}", {"id": todo["id"]}


def _do_delete(query: str) -> tuple[str, dict | None]:
    """Delete a todo identified by the query."""
    todos = list_todos(include_done=True)
    todo, err = _find_todo(query, todos)
    if err:
        return err, None
    delete_todo(todo["id"])
    log.info("todo deleted id=%s text=%r", todo["id"], todo["text"][:60])
    return f"Deleted: {todo['text']}", {"id": todo["id"]}


_HANDLERS = {
    "list":   _do_list,
    "add":    _do_add,
    "done":   _do_done,
    "delete": _do_delete,
}


class TodoModule(BaseModule):
    name = "todo"
    description = (
        "Manages the user's todo / task list: showing pending tasks, adding new "
        "todos, marking them done (with verification), and deleting them."
    )

    def __init__(self):
        """Ensure the todos table exists before first use."""
        ensure_table()

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        """Route a todo query to its handler by intent (no LLM for routing)."""
        intent = _intent(query)
        log.info("intent=%s q=%r", intent, query[:80])
        handler = _HANDLERS[intent]
        text, data = handler() if intent == "list" else handler(query)

        follow_up = None
        if intent == "list" and data and data.get("count"):
            follow_up = "Say \"add todo <text>\" or \"mark <text> done\" to update it."
        return ModuleResponse(text=text, module=self.name, data=data, follow_up=follow_up)
