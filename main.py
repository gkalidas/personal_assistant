"""GK Personal Assistant — terminal chat entry point."""

import os
import time
from dotenv import load_dotenv

load_dotenv()

from core import memory
from core.router import dispatch
from modules.finance.module import FinanceModule
from modules.farming.module import FarmingModule


MODULES = {
    "finance": FinanceModule(),
    "farming": FarmingModule(),
}

GOODBYE = {"exit", "quit", "bye", "/exit", "/quit"}


def print_response(responses):
    for r in responses:
        print(f"\nGK [{r.module}]: {r.text}")
        if r.follow_up:
            print(f"\n  → {r.follow_up}")


def main():
    memory.init_db()
    profile = memory.load_profile()

    print("Welcome to the future, GK.")
    print('Type your query. "exit" to quit.\n')

    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGK: Goodbye.")
            break

        if not query:
            continue
        if query.lower() in GOODBYE:
            print("GK: Goodbye.")
            break

        context = {"profile": profile}
        event_id = memory.log_query_start(query)
        t0 = time.monotonic()

        try:
            responses = dispatch(query, MODULES, context)
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            memory.log_query_done(event_id, "router", str(e), latency_ms, status="error")
            print(f"\nGK: Something went wrong — {e}")
            continue

        latency_ms = int((time.monotonic() - t0) * 1000)
        print_response(responses)

        for r in responses:
            memory.log_query_done(
                event_id=event_id,
                module=r.module,
                response=r.text,
                latency_ms=latency_ms,
                metadata=r.data,
            )

        print()


if __name__ == "__main__":
    main()
