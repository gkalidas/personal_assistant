from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ModuleResponse:
    text: str
    module: str
    data: dict[str, Any] | None = None
    follow_up: str | None = None


class BaseModule(ABC):
    name: str
    description: str  # used by router to decide when to invoke this module

    @abstractmethod
    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        """Process a query and return a response. context carries user profile + session state."""
        ...

    def can_handle(self, query: str) -> bool:
        """Optional fast pre-filter before router decision. Default: always yes."""
        return True
