from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class WorkflowProvider(ABC):
    name: str

    @abstractmethod
    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        """Generate one workflow stage and return JSON-serializable content."""

