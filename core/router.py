"""Query router — picks which module(s) handle a message.

Tries the fast embedding router first (~1 ms, no LLM); falls back to a small
LLM classifier. On disagreement between the two, logs a routing mismatch.
"""

import json
import logging
import time
import httpx

from core.config import OLLAMA_URL, ROUTER_MODEL, CHAT_KEEP_ALIVE
from core.base_module import BaseModule, ModuleResponse

log = logging.getLogger(__name__)

# Short keyword hints per module — easier for a 0.5b router model than long prose.
_MODULE_KEYWORDS = {
    "finance": "money, expenses, income, budget, savings, loans, spent, earned, SIP, EMI, tax",
    "farming": "crops, weather, spray, soil, disease, farm, harvest, rain, plot, fertilizer, mandi, price, market rate, APMC",
    "health":  "BP, blood pressure, steps, weight, sleep, sugar, glucose, health, walked, kg, hours slept",
    "system":  "system load, CPU, RAM, busy, idle, load pattern, heatmap, security guardian, CVE scan, threat intel, anomaly, audit, background tasks, task schedule",
    "diary":   "diary, photos, journal, write diary, photo diary, show diary, approve diary, draft, daily log, weekly summary, week review, this week",
    "search":  "search, news, latest, current events, what is, who is, government scheme, policy, regulation, internet, web, find out, look up",
    "code":    "analyze code, codebase, lines of code, LOC, complexity, security scan code, what does this directory do, explain module, file breakdown",
    "todo":    "todo, to-do, task, tasks, to-do list, task list, my list, add todo, remind me to, mark done, complete task, finish task, delete todo, pending tasks, things to do",
    "general": "greetings, hello, hi, thanks, small talk, chit-chat, everyday questions, general knowledge, how are you, who are you, anything not covered by the other modules",
}

# Safe fallback when the router can't confidently pick a domain module.
# Routing to a single conversational handler avoids fanning a query out to
# every module at once (which overloads Ollama and causes timeout storms).
_FALLBACK_MODULE = "general"

# Max seconds to wait for the LLM router before failing fast to the fallback.
# The 0.5b router answers in well under a second when Ollama is warm; waiting
# longer means Ollama is stuck, so we stop instead of hanging for minutes.
_ROUTER_TIMEOUT = 30.0



_SYSTEM_PROMPT = """Route the user message to the SINGLE most relevant module.
Pick exactly ONE — the best fit. Reply ONLY with JSON.

Modules:
{module_list}

Examples:
"spent 500 on seeds" -> {{"modules": ["finance"]}}
"when to spray pomegranate" -> {{"modules": ["farming"]}}
"weather today" -> {{"modules": ["farming"]}}
"monsoon affecting my budget" -> {{"modules": ["finance"]}}
"what can you do for me" -> {{"modules": ["general"]}}
"BP was 130/85" -> {{"modules": ["health"]}}
"walked 9000 steps" -> {{"modules": ["health"]}}
"slept 6 hours" -> {{"modules": ["health"]}}
"hello" -> {{"modules": ["general"]}}
"is the system busy?" -> {{"modules": ["system"]}}
"show load pattern" -> {{"modules": ["system"]}}
"show CPU and RAM usage" -> {{"modules": ["system"]}}
"security scan results" -> {{"modules": ["system"]}}
"when does guardian run?" -> {{"modules": ["system"]}}
"is system idle?" -> {{"modules": ["system"]}}
"pomegranate price at mandi today" -> {{"modules": ["farming"]}}
"write diary from my photos" -> {{"modules": ["diary"]}}
"show my diary draft" -> {{"modules": ["diary"]}}
"approve diary" -> {{"modules": ["diary"]}}
"list diary drafts" -> {{"modules": ["diary"]}}
"diary for 2025-08-03" -> {{"modules": ["diary"]}}
"weekly summary" -> {{"modules": ["diary"]}}
"what did I do this week" -> {{"modules": ["diary"]}}
"week in review" -> {{"modules": ["diary"]}}
"latest news about farming subsidies" -> {{"modules": ["search"]}}
"what is the PM-KISAN scheme?" -> {{"modules": ["search"]}}
"search for pomegranate export prices" -> {{"modules": ["search"]}}
"today's gold price in India" -> {{"modules": ["search"]}}
"who is the agriculture minister?" -> {{"modules": ["search"]}}
"show todo list" -> {{"modules": ["todo"]}}
"what are my tasks" -> {{"modules": ["todo"]}}
"add todo buy seeds" -> {{"modules": ["todo"]}}
"remind me to call the vet" -> {{"modules": ["todo"]}}
"mark the pump task done" -> {{"modules": ["todo"]}}
"delete the searxng todo" -> {{"modules": ["todo"]}}

Reply format: {{"modules": ["name"]}}  (exactly one name)"""


def _build_system_prompt(modules: dict[str, BaseModule]) -> str:
    """Render the router system prompt with a keyword hint line per active module."""
    lines = [f"- {name}: {_MODULE_KEYWORDS.get(name, name)}" for name in modules]
    return _SYSTEM_PROMPT.format(module_list="\n".join(lines))


def _embed_route(query: str, modules: dict[str, BaseModule]) -> str | None:
    """Fast embedding-based route (~1 ms). Returns a module name, or None to fall back."""
    try:
        from core.embedding_router import fast_route
        choice = fast_route(query)
        if choice and choice in modules:
            return choice
    except Exception as e:
        log.debug("embed fast-path skip: %s", e)
    return None


def _log_routing_mismatch(query: str, embed_choice: str, routed: list[str]) -> None:
    """Record a low-severity mistake when the embedding and LLM routers disagree."""
    if not (embed_choice and routed and embed_choice not in routed):
        return
    try:
        from core.mistake_log import log_mistake
        log_mistake("routing_mismatch", query=query[:300],
                    details={"embed": embed_choice, "llm": routed}, severity="low")
    except Exception:
        pass


def route(query: str, modules: dict[str, BaseModule]) -> list[str]:
    """Return the list of module names that should handle this query."""
    embed_choice = _embed_route(query, modules)
    if embed_choice:
        log.info("embed-route → %s  q=%r", embed_choice, query[:80])
        return [embed_choice]

    payload = {
        "model": ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _build_system_prompt(modules)},
            {"role": "user", "content": query},
        ],
        "stream": False,
        "format": "json",
        "keep_alive": CHAT_KEEP_ALIVE,   # keep the tiny router model warm between queries
    }

    t0 = time.monotonic()
    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=_ROUTER_TIMEOUT)
        resp.raise_for_status()
        chosen = json.loads(resp.json()["message"]["content"]).get("modules", [])
        valid  = [m for m in chosen if m in modules]
        # One query → one module: take the router's top pick, else the safe fallback.
        routed = [valid[0]] if valid else [_fallback(modules)]
        log.info("route → %s  (%dms)  q=%r", routed, int((time.monotonic() - t0) * 1000), query[:80])
        _log_routing_mismatch(query, embed_choice, routed)
        return routed
    except Exception as e:
        log.error("router LLM failed (%dms): %s", int((time.monotonic() - t0) * 1000), e)
        return [_fallback(modules)]


def _fallback(modules: dict[str, BaseModule]) -> str:
    """Single safe module to handle queries the router couldn't classify."""
    return _FALLBACK_MODULE if _FALLBACK_MODULE in modules else next(iter(modules))


def dispatch(
    query: str,
    modules: dict[str, BaseModule],
    context: dict,
) -> list[ModuleResponse]:
    """Route query and dispatch to chosen modules. Returns their responses."""
    chosen = route(query, modules)
    responses = []
    for name in chosen:
        if name in modules:
            responses.append(modules[name].handle(query, context))
    return responses
