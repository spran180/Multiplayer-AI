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

    # Groq guarantees schema-conforming output for these models in strict mode.
    # Keep JSON Object Mode for custom models with no structured-output support.
    _STRICT_JSON_MODELS = {
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "qwen/qwen3.8-27b",
    }

    _STEP_SCHEMA = {
        "type": "object",
        "properties": {
            "thought": {"type": "string"},
            "tool": {
                "type": ["string", "null"],
                "enum": ["search", "calculate", "verify", "apply_change", "finish", None],
            },
            # Strict structured outputs require closed objects. Keep the union
            # of arguments supported by our tools here, then strip null fields.
            "tool_args": {
                "type": "object",
                "properties": {
                    "query": {"type": ["string", "null"]},
                    "offline": {"type": ["boolean", "null"]},
                    "expr": {"type": ["string", "null"]},
                    "actual": {"type": ["string", "number", "boolean", "null"]},
                    "expected": {"type": ["string", "number", "boolean", "null"]},
                    "description": {"type": ["string", "null"]},
                },
                "required": ["query", "offline", "expr", "actual", "expected", "description"],
                "additionalProperties": False,
            },
            "finish": {"type": "boolean"},
            "summary": {"type": ["string", "null"]},
            "memory_note": {"type": ["string", "null"]},
        },
        "required": ["thought", "tool", "tool_args", "finish", "summary", "memory_note"],
        "additionalProperties": False,
    }

    _MERGE_SCHEMA = {
        "type": "object",
        "properties": {"merged_output": {"type": "string"}},
        "required": ["merged_output"],
        "additionalProperties": False,
    }

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 timeout: float = 45.0):
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        if not self.api_key:
            raise ValueError("GROQ_API_KEY is required to use the Groq planner")
        self.model = model or os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        self.timeout = timeout

    def _response_format(self, name: str, schema: dict) -> dict:
        if self.model in self._STRICT_JSON_MODELS:
            return {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            }
        return {"type": "json_object"}

    def plan(self, task: str, memory: List[MemoryEntry], step_index: int) -> StepDecision:
        import httpx

        transcript = "\n".join(f"[{m.kind}] {m.text}" for m in memory[-16:])
        body = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": self._response_format("agent_step", self._STEP_SCHEMA),
            "messages": [
                {"role": "system", "content": (
                    "You are a careful task agent. Return a JSON object with exactly these keys: "
                    "thought (string), tool (search, calculate, verify, apply_change, finish, or null), "
                    "tool_args (object with keys query, offline, expr, actual, expected, description; "
                    "use null for unused arguments), finish (boolean), summary (string or null), and "
                    "memory_note (string or null). Include every key. Use finish=true when done. "
                    "For research tasks, use search to gather source URLs and snippets before writing conclusions; "
                    "search results are evidence, and final summaries should cite their URLs. "
                    "Never claim a tool ran unless requested through tool. Use apply_change "
                    "to propose external side effects; that tool requires human approval." )},
                {"role": "user", "content": f"Task: {task}\nStep: {step_index}\nMemory:\n{transcript}"},
            ],
        }
        response = self._post_with_retry(httpx, body)
        self._raise_for_status(response)
        raw = response.json()["choices"][0]["message"]["content"]
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"Groq returned an invalid planner response for model {self.model}: {str(raw)[:500]}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Groq returned a non-object planner response for model {self.model}")
        tool = data.get("tool")
        if tool not in (None, "search", "calculate", "verify", "apply_change", "finish"):
            tool = None
        return StepDecision(
            thought=str(data.get("thought", "")), tool=tool,
            tool_args={k: v for k, v in data.get("tool_args", {}).items() if v is not None}
            if isinstance(data.get("tool_args"), dict) else {},
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
            "response_format": self._response_format("merge_result", self._MERGE_SCHEMA),
        }
        response = self._post_with_retry(httpx, body)
        self._raise_for_status(response)
        content = response.json()["choices"][0]["message"]["content"]
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"Groq returned an invalid merge response for model {self.model}: {str(content)[:500]}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Groq returned a non-object merge response for model {self.model}")
        return str(data.get("merged_output", "")).strip() or super().merge_outputs(left, right)

    @staticmethod
    def _raise_for_status(response) -> None:
        """Keep Groq's actionable error message instead of only its HTTP status."""
        if response.is_success:
            return

        message = ""
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            if isinstance(error, dict):
                message = str(error.get("message") or "")
                failed_generation = error.get("failed_generation")
                if failed_generation:
                    message += f"; failed_generation={json.dumps(failed_generation, ensure_ascii=False)}"
        except (ValueError, TypeError):
            pass

        if not message:
            message = response.text[:1000].strip()
        detail = f": {message}" if message else ""
        raise RuntimeError(
            f"Groq rejected the request (HTTP {response.status_code}){detail}"
        )

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
