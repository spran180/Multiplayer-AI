"""LLM planner protocol — the seam between the agent loop and any provider.

The loop only ever calls :meth:`plan` and receives a :class:`StepDecision`.
Providers (offline :class:`~backend.agent.llm.mock.MockPlanner`, Groq, ...) are
interchangeable. Keep the decision small and provider-agnostic so the loop and
the state graph don't care which model is driving them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.protocols.python.protocols import MemoryEntry


@dataclass
class StepDecision:
    """One step the planner wants the loop to take."""

    thought: str
    tool: Optional[str] = None
    tool_args: Dict[str, Any] = field(default_factory=dict)
    finish: bool = False
    summary: Optional[str] = None
    memory_note: Optional[str] = None  # optional observation to fold into memory


class LLMClient(ABC):
    name: str = "base"

    @abstractmethod
    def plan(self, task: str, memory: List[MemoryEntry], step_index: int) -> StepDecision:
        """Produce the next step. ``memory`` is the running working-memory list."""

    def close(self) -> None:  # optional resource cleanup
        pass

    def merge_outputs(self, left: str, right: str) -> str:
        """Reconcile two branch outputs without discarding their provenance."""
        if left == right:
            return left
        return f"Branch A:\n{left}\n\nBranch B:\n{right}"
