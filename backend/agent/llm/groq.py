"""Groq Chat Completions adapter. Expects one JSON StepDecision per response."""
from __future__ import annotations

import json
import os
import time
from typing import List

from shared.protocols.python.protocols import MemoryEntry
from .base import LLMClient, StepDecision


class GroqPlanner(LLMClient):
    name = "groq"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 timeout: float = 45.0):
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        if not self.api_key:
            raise ValueError("GROQ_API_KEY is required to use the Groq planner")
        self.model = model or os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        self.timeout = timeout

    def plan(self, task: str, memory: List[MemoryEntry], step_index: int) -> StepDecision:
        import httpx

        transcript = "\n".join(f"[{m.kind}] {m.text}" for m in memory[-16:])
        body = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": (
                    "You are a careful task agent. Return JSON only, with keys thought, "
                    "tool (one of search, calculate, verify, apply_change, finish or null), tool_args, "
                    "finish (boolean), summary, memory_note. Use finish=true when done. "
                    "For research tasks, use search to gather source URLs and snippets before writing conclusions; "
                    "search results are evidence, and final summaries should cite their URLs. "
                    "Never claim a tool ran unless requested through tool. Use apply_change "
                    "to propose external side effects; that tool requires human approval." )},
                {"role": "user", "content": f"Task: {task}\nStep: {step_index}\nMemory:\n{transcript}"},
            ],
        }
        response = self._post_with_retry(httpx, body)
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
        data = json.loads(raw)
        tool = data.get("tool")
        if tool not in (None, "search", "calculate", "verify", "apply_change", "finish"):
            tool = None
        return StepDecision(
            thought=str(data.get("thought", "")), tool=tool,
            tool_args=data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {},
            finish=bool(data.get("finish", tool == "finish")),
            summary=str(data.get("summary") or "") or None,
            memory_note=str(data.get("memory_note") or "") or None,
        )

    def merge_outputs(self, left: str, right: str) -> str:
        import httpx
        body = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": (
                    "Merge two alternative agent outputs into one concise result. Preserve "
                    "compatible findings, clearly call out unresolved disagreements, and "
                    "do not invent evidence. Return JSON with a single merged_output string." )},
                {"role": "user", "content": f"BRANCH A:\n{left[:6000]}\n\nBRANCH B:\n{right[:6000]}"},
            ],
            "response_format": {"type": "json_object"},
        }
        response = self._post_with_retry(httpx, body)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return str(json.loads(content).get("merged_output", "")).strip() or super().merge_outputs(left, right)

    def _post_with_retry(self, httpx, body):
        """Retry transient provider limits/outages, honoring Groq's Retry-After."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        retryable = {429, 500, 502, 503, 504}
        for attempt in range(4):
            response = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions", json=body,
                headers=headers, timeout=self.timeout,
            )
            if response.status_code not in retryable:
                return response

            retry_after = response.headers.get("retry-after")
            try:
                delay = float(retry_after) if retry_after is not None else min(2 ** attempt, 8)
            except ValueError:
                delay = min(2 ** attempt, 8)
            if response.status_code == 429 and (attempt == 3 or delay > 120):
                response.close()
                wait_note = f" Retry-After is {int(delay)} seconds." if retry_after is not None else ""
                raise RuntimeError(
                    "Groq is still rate-limiting this request after retries." + wait_note +
                    " Wait for the model quota to reset or check the Groq Console rate limits."
                )
            if attempt == 3:
                return response
            response.close()
            time.sleep(max(0.5, delay))
        raise RuntimeError("Groq request retry loop ended unexpectedly")
