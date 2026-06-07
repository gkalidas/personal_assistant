import json
import os
import httpx
from core.base_module import BaseModule, ModuleResponse


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "qwen2.5:3b")

_SYSTEM_PROMPT = """You are a routing classifier for a personal assistant. Given a user message, decide which module(s) should handle it.

Available modules:
{module_descriptions}

Rules:
- Respond ONLY with valid JSON.
- If one module fits, return: {{"modules": ["module_name"]}}
- If the query spans multiple modules (e.g. "adjust farming budget because of monsoon"), return both: {{"modules": ["finance", "farming"]}}
- If no module fits, return: {{"modules": ["general"]}}
- Do not explain. JSON only."""


def _build_system_prompt(modules: dict[str, BaseModule]) -> str:
    descriptions = "\n".join(
        f"- {name}: {mod.description}" for name, mod in modules.items()
    )
    return _SYSTEM_PROMPT.format(module_descriptions=descriptions)


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
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        result = json.loads(content)
        chosen = result.get("modules", ["general"])
        # validate — only return modules that actually exist
        valid = [m for m in chosen if m in modules]
        return valid if valid else ["general"]
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
