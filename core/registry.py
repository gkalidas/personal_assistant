"""Single source of truth for the assistant's module registry.

Both the terminal entry point (main.py) and the dashboard build their module
set from here, so routing and the router's safe fallback behave identically in
both. Previously each maintained its own dict and they drifted: the dashboard
was missing `general` (so greetings/unmatched queries had no handler), and the
terminal was missing `code`.
"""

from core.base_module import BaseModule

# Canonical module names, in registry order. Kept as a cheap constant so callers
# (e.g. the dashboard service-health panel) can list modules without paying the
# cost of importing/instantiating them. build_modules() asserts it stays in sync.
MODULE_NAMES = ("finance", "farming", "health", "system", "diary", "search", "todo", "general", "code", "personal")


def build_modules() -> dict[str, BaseModule]:
    """Instantiate and return the full module registry, keyed by name.

    Imports are local so callers that build this lazily (e.g. the dashboard)
    don't pay the import cost until first use.
    """
    from modules.finance.module import FinanceModule
    from modules.farming.module import FarmingModule
    from modules.health.module import HealthModule
    from modules.system.module import SystemModule
    from modules.diary.module import DiaryModule
    from modules.search.module import SearchModule
    from modules.todo.module import TodoModule
    from modules.general.module import GeneralModule
    from modules.code.module import CodeModule
    from modules.personal.module import PersonalModule

    modules = {
        "finance": FinanceModule(),
        "farming": FarmingModule(),
        "health":  HealthModule(),
        "system":  SystemModule(),
        "diary":   DiaryModule(),
        "search":  SearchModule(),
        "todo":    TodoModule(),
        "general": GeneralModule(),
        "code":    CodeModule(),
        "personal": PersonalModule(),
    }
    assert tuple(modules) == MODULE_NAMES, "MODULE_NAMES out of sync with build_modules()"
    return modules
