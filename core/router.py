import json
import logging
import time
import httpx

from core.config import OLLAMA_URL, ROUTER_MODEL
from core.base_module import BaseModule, ModuleResponse

log = logging.getLogger(__name__)



_SYSTEM_PROMPT = """Route the user message to the correct module. Reply ONLY with JSON.

Modules:
{module_list}

Examples:
"spent 500 on seeds" -> {{"modules": ["finance"]}}
"when to spray pomegranate" -> {{"modules": ["farming"]}}
"weather today" -> {{"modules": ["farming"]}}
"monsoon affecting my budget" -> {{"modules": ["finance", "farming"]}}
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

Reply format: {{"modules": ["name"]}}"""


def _build_system_prompt(modules: dict[str, BaseModule]) -> str:
    # Short keyword list — easier for a 0.5b model than long descriptions
    keywords = {
        "finance": "money, expenses, income, budget, savings, loans, spent, earned, SIP, EMI, tax",
        "farming": "crops, weather, spray, soil, disease, farm, harvest, rain, plot, fertilizer, mandi, price, market rate, APMC",
        "health":  "BP, blood pressure, steps, weight, sleep, sugar, glucose, health, walked, kg, hours slept",
        "system":  "system load, CPU, RAM, busy, idle, load pattern, heatmap, security guardian, CVE scan, threat intel, anomaly, audit, background tasks, task schedule",
        "diary":   "diary, photos, journal, write diary, photo diary, show diary, approve diary, draft, daily log, weekly summary, week review, this week",
    }
    lines = []
    for name in modules:
        hint = keywords.get(name, name)
        lines.append(f"- {name}: {hint}")
    return _SYSTEM_PROMPT.format(module_list="\n".join(lines))


def route(query: str, modules: dict[str, BaseModule]) -> list[str]:
    """Return list of module names that should handle this query."""
    system = _build_system_prompt(modules)
    payload = {
        "model": ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        "stream": False,
        "format": "json",
    }

    t0 = time.monotonic()
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=120.0,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        result = json.loads(content)
        chosen = result.get("modules", [])
        valid = [m for m in chosen if m in modules]
        routed = valid if valid else list(modules.keys())
        log.info("route → %s  (%dms)  q=%r",
                 routed, int((time.monotonic() - t0) * 1000), query[:80])
        return routed
    except Exception as e:
        log.error("router LLM failed (%dms): %s", int((time.monotonic() - t0) * 1000), e)
        return list(modules.keys())


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
