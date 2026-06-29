"""
General conversation module — handles greetings, small talk, and everyday
questions that don't belong to a specialized domain (finance, farming, health,
system, diary, search).

This is also the router's safe fallback: when no domain module matches, the
query lands here for a single quick chat reply — instead of fanning out to
every module at once (which overloads Ollama and times out).
"""

import logging
from typing import Any

from core.base_module import BaseModule, ModuleResponse

log = logging.getLogger(__name__)

_SYSTEM = """You are GK, a friendly personal assistant for Ganesh, a farmer in Maharashtra, India.

You handle casual conversation and everyday questions. Be warm, brief, and natural.

RULES:
- Keep replies short (1-3 sentences) unless asked for more.
- For greetings, reply naturally and, if useful, mention you can help with farming,
  finances, health, the diary, system status, or web search.
- Answer general-knowledge questions directly when you're confident.
- If a question clearly needs live/current information (today's news, prices,
  recent events) say you can look it up — suggest the user ask to "search" for it.
- Never invent personal data about Ganesh (his money, health readings, farm logs).
  Point him to the relevant feature instead.

Respond in plain text. No JSON."""


def _user_name(context: dict) -> str:
    profile = context.get("profile", {}) or {}
    return profile.get("name") or profile.get("alias") or "Ganesh"


class GeneralModule(BaseModule):
    name = "general"
    description = (
        "Handles greetings, small talk, and everyday/general-knowledge questions "
        "that don't fit finance, farming, health, system, diary, or search. "
        "Also the safe fallback when no other module matches."
    )

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        """Single quick LLM chat reply for casual / general queries."""
        from core.llm import call as llm_call

        system = _SYSTEM + f"\n\nYou are talking to {_user_name(context)}."
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": query},
        ]
        # Stream to stdout only for the interactive terminal; API/voice callers
        # pass stream_to_stdout=False so the reply isn't dumped into server logs.
        stream = bool(context.get("stream_to_stdout", True))
        try:
            text = llm_call(
                messages,
                think=False,
                stream_to_stdout=stream,
                prefix="\nGK [general]: ",
                use_fallback=True,
            )
            return ModuleResponse(text=text, module=self.name, streamed=stream)
        except Exception as e:
            log.error("general chat failed: %s", e)
            from core.mistake_log import log_mistake
            log_mistake("llm_timeout", query=query, module=self.name,
                        details=str(e), severity="medium")
            return ModuleResponse(
                text="I'm having trouble responding right now — the local model may be busy. Try again in a moment.",
                module=self.name,
            )
