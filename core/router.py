import json
import os
import httpx
from core.base_module import BaseModule, ModuleResponse


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "qwen2.5:0.5b")

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

Reply format: {{"modules": ["name"]}}"""


def _build_system_prompt(modules: dict[str, BaseModule]) -> str:
    # Short keyword list — easier for a 0.5b model than long descriptions
    keywords = {
        "finance": "money, expenses, income, budget, savings, loans, spent, earned, SIP, EMI, tax",
        "farming": "crops, weather, spray, soil, disease, farm, harvest, rain, plot, fertilizer",
        "health":  "BP, blood pressure, steps, weight, sleep, sugar, glucose, health, walked, kg, hours slept",
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
        # "general" or unknown → try all modules
        return valid if valid else list(modules.keys())
    except Exception as e:
        # fallback: let all modules try to handle it
        print(f"[router error] {e}")
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
