"""Planner adapter for locally hosted Ollama models."""
from __future__ import annotations

import json
import os
from typing import List

from shared.protocols.python.protocols import MemoryEntry
from .base import LLMClient, StepDecision


class OllamaPlanner(LLMClient):
    """Use Ollama's local chat API to produce one structured agent decision."""

    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float | None = None):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL") or
                         "http://127.0.0.1:11434").rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        self.timeout = timeout or float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))
        self.name = f"ollama:{self.model}"

    def plan(self, task: str, memory: List[MemoryEntry], step_index: int) -> StepDecision:
        import httpx

        transcript = "\n".join(f"[{entry.kind}] {entry.text}" for entry in memory[-16:])
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
            "messages": [
                {"role": "system", "content": (
                    "You are a careful task agent. Return one JSON object with keys thought, "
                    "tool, tool_args, finish, summary, and memory_note. The tool must be one "
                    "of search, calculate, verify, apply_change, finish, or null. Use finish=true "
                    "when done. Never claim a tool ran unless requested through tool. Use "
                    "apply_change to propose external side effects; that tool requires human approval."
                )},
                {"role": "user", "content": f"Task: {task}\nStep: {step_index}\nMemory:\n{transcript}"},
            ],
        }
        try:
            response = httpx.post(
                f"{self.base_url}/api/chat", json=body, timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.base_url}. Start Ollama, pull {self.model}, "
                "and check OLLAMA_BASE_URL."
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RuntimeError(
                    f"Ollama model '{self.model}' is not available. Pull it with: ollama pull {self.model}"
                ) from exc
            raise RuntimeError(
                f"Ollama returned HTTP {exc.response.status_code}: {exc.response.text[:500]}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Ollama did not answer within {self.timeout:g} seconds. Increase OLLAMA_TIMEOUT_SECONDS "
                "or use a smaller local model."
            ) from exc

        try:
            raw = response.json()["message"]["content"]
            data = json.loads(raw)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Ollama returned an invalid JSON agent decision.") from exc

        tool = data.get("tool")
        if tool not in (None, "search", "calculate", "verify", "apply_change", "finish"):
            tool = None
        finish = data.get("finish") is True or tool == "finish"
        return StepDecision(
            thought=str(data.get("thought") or ""),
            tool=tool,
            tool_args=data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {},
            finish=finish,
            summary=str(data.get("summary") or "") or None,
            memory_note=str(data.get("memory_note") or "") or None,
        )

    def merge_outputs(self, left: str, right: str) -> str:
        # Keep branch merging available offline without requiring another model call.
        return super().merge_outputs(left, right)
