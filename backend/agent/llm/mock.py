"""Offline, deterministic planner — the default so the platform runs with no key.

It scripts a believable multi-step run (plan -> search -> compute -> verify ->
finish) so you can exercise real-time streaming, steering, and graph versioning
without any external model. Swap in Groq (or any LLMClient) via config to drive
real planning.
"""

from __future__ import annotations

from typing import List

from shared.protocols.python.protocols import MemoryEntry
from .base import LLMClient, StepDecision


class MockPlanner(LLMClient):
    name = "mock"

    def __init__(self, horizon: int = 5):
        self.horizon = horizon  # number of steps before finishing

    def plan(self, task: str, memory: List[MemoryEntry], step_index: int) -> StepDecision:
        risky_terms = ("deploy", "production", "delete", "drop database", "write to database", "send payment")
        if step_index == 0 and any(term in task.lower() for term in risky_terms):
            return StepDecision(
                thought="This request could change an external system. I will prepare a proposal for human review.",
                tool="apply_change", tool_args={"description": task},
                memory_note="A protected action is waiting for approval.",
            )
        if step_index == 0:
            return StepDecision(
                thought=f"Breaking down the task: {task!r}. I'll gather context, "
                        f"compute a result, then verify before concluding.",
                tool="search",
                tool_args={"query": task, "offline": True},
                memory_note="Started; gathering context.",
            )
        if step_index == 1:
            return StepDecision(
                thought="I have the context. Now I'll compute a concrete result.",
                tool="calculate",
                tool_args={"expr": "42 * 2"},
                memory_note="Computed a candidate result.",
            )
        if step_index == 2:
            return StepDecision(
                thought="Cross-checking the result against the constraints in the task.",
                tool="verify",
                tool_args={"actual": 84, "expected": 84},
                memory_note="Verification passed.",
            )
        if step_index >= self.horizon - 1:
            return StepDecision(
                thought="All checks pass. Finalizing the answer.",
                finish=True,
                summary=f"Completed: {task}. Steps executed: {step_index + 1}.",
            )
        return StepDecision(
            thought=f"Step {step_index + 1}: refining the working hypothesis.",
            memory_note=f"Refinement {step_index}.",
        )
