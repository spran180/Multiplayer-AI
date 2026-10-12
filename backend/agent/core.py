"""Async agent loop with distributed steering and multi-person approval gates."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import asdict
from typing import Any, Callable

from shared.protocols.python.protocols import MemoryEntry, RunState, Snapshot
from backend.agent.llm.base import LLMClient
from backend.agent.tools import run_tool
from backend.state.snapshot import stamp

SIDE_EFFECT_TOOLS = {"apply_change"}


class AIAgentCore:
    def __init__(self, room, planner: LLMClient,
                 emit: Callable[[str, dict[str, Any]], Any],
                 max_steps=12, approval_quorum=2, approval_timeout=300):
        self.room, self.planner, self.emit = room, planner, emit
        self.max_steps = max_steps
        self.approval_quorum = max(1, approval_quorum)
        self.approval_timeout = max(10, approval_timeout)

    async def run(self, task: str):
        loop = asyncio.get_running_loop()
        assert self.room.pause_event is not None and self.room.interrupt_event is not None
        topic = f"room:{self.room.id}"

        def receive(message):
            if isinstance(message, str):
                try:
                    message = json.loads(message)
                except json.JSONDecodeError:
                    return
            if not isinstance(message, dict):
                return
            kind = message.get("type")
            payload = message.get("payload") or {}
            if kind == "steering.status":
                loop.call_soon_threadsafe(self._apply_steering, payload)
            elif kind == "approval.updated":
                loop.call_soon_threadsafe(self._resolve_approval, payload)

        self.room.bus.subscribe(topic, receive)
        self._set_state(RunState.RUNNING)
        self.room.pause_event.set()
        self.room.interrupt_event.clear()
        lease_heartbeat = asyncio.create_task(self._renew_run_lease())
        memories = [MemoryEntry(kind="user", text=task)]
        steps = 0
        try:
            await self._checkpoint("Task started", memories)
            self.emit("agent.started", {"task": task, "model": self.planner.name})
            for step_index in range(self.max_steps):
                await self.room.pause_event.wait()
                if self.room.interrupt_event.is_set():
                    self._set_state(RunState.INTERRUPTED)
                    self.emit("agent.done", {"state": "interrupted", "steps": step_index})
                    return

                steps = step_index + 1

                injections, self.room.injections = self.room.injections, []
                for note in injections:
                    memories.append(MemoryEntry(kind="injection", text=note))
                    self.emit("steering.status", {"action": "inject.applied", "text": note})

                try:
                    env_keys = sorted(self.room.store.get_runtime_env(self.room.id))
                    planner_memory = list(memories)
                    if env_keys:
                        planner_memory.append(MemoryEntry(
                            kind="system", text="Runtime variables available to tools: " + ", ".join(env_keys)))
                    decision = await asyncio.to_thread(self.planner.plan, task, planner_memory, step_index)
                except Exception as exc:
                    self._set_state(RunState.FAILED)
                    self.emit("agent.error", {"message": f"Planner failed: {exc}"})
                    self.emit("agent.done", {"state": "failed", "steps": steps})
                    return
                if self.room.interrupt_event.is_set():
                    self._set_state(RunState.INTERRUPTED)
                    self.emit("agent.done", {"state": "interrupted", "steps": steps})
                    return

                if decision.thought:
                    memories.append(MemoryEntry(kind="thought", text=decision.thought))
                    self.emit("agent.thought", {"text": decision.thought, "step_index": step_index})
                    await self._checkpoint(decision.thought[:120], memories)
                if decision.memory_note:
                    memories.append(MemoryEntry(kind="system", text=decision.memory_note))

                if decision.finish or decision.tool == "finish":
                    final = decision.summary or decision.thought or "Task complete."
                    memories.append(MemoryEntry(kind="assistant", text=final))
                    await self._checkpoint("Task completed", memories, RunState.FINISHED)
                    self._set_state(RunState.FINISHED)
                    self.emit("agent.done", {"state": "finished", "summary": final, "steps": step_index + 1})
                    return

                if not decision.tool:
                    continue
                call = {"name": decision.tool, "args": decision.tool_args}
                self.emit("agent.tool_call", {"tool": call, "step_index": step_index})

                if decision.tool in SIDE_EFFECT_TOOLS:
                    approved = await self._request_approval(decision.tool, decision.tool_args)
                    if not approved:
                        memories.append(MemoryEntry(kind="approval", text=f"Action {decision.tool} was not approved."))
                        await self._checkpoint(f"Rejected {decision.tool}", memories)
                        continue

                runtime_env = {}
                try:
                    runtime_env = self.room.store.get_runtime_env(self.room.id)
                    resolved_args = _expand_env(decision.tool_args, runtime_env)
                    # Search and other network-backed tools must not block the
                    # event loop: room presence, steering, and websocket events
                    # continue while a tool is waiting on an external service.
                    tool_result = await asyncio.to_thread(run_tool, decision.tool, resolved_args)
                    result = _redact(tool_result, runtime_env)
                    memories.append(MemoryEntry(kind="tool", text=f"{decision.tool}: {result}"))
                    self.emit("agent.tool_result", {"tool": decision.tool, "result": result,
                                                     "step_index": step_index})
                except Exception as exc:
                    safe_error = _redact(str(exc), runtime_env)
                    memories.append(MemoryEntry(kind="tool", text=f"{decision.tool} failed: {safe_error}"))
                    self.emit("agent.error", {"message": f"Tool {decision.tool} failed: {safe_error}"})
                await self._checkpoint(f"Ran {decision.tool}", memories)

            final = "Stopped after reaching the configured step limit."
            memories.append(MemoryEntry(kind="assistant", text=final))
            await self._checkpoint("Step limit reached", memories, RunState.FINISHED)
            self._set_state(RunState.FINISHED)
            self.emit("agent.done", {"state": "finished", "summary": final, "steps": self.max_steps})
        except asyncio.CancelledError:
            self._set_state(RunState.INTERRUPTED)
            self.emit("agent.done", {"state": "interrupted", "steps": steps})
            raise
        except Exception as exc:
            self._set_state(RunState.FAILED)
            self.emit("agent.error", {"message": f"Agent runtime failed: {exc}"})
            self.emit("agent.done", {"state": "failed", "steps": steps})
        finally:
            lease_heartbeat.cancel()
            try:
                await lease_heartbeat
            except asyncio.CancelledError:
                pass
            self.room.bus.unsubscribe(topic, receive)
            if self.room.run_lease_token:
                self.room.store.release_run(self.room.id, self.room.run_lease_token)
                self.room.run_lease_token = None
            self.room.run_task = None

    async def _renew_run_lease(self):
        """Keep the shared run lease alive while planning or awaiting review."""
        while True:
            await asyncio.sleep(30)
            token = self.room.run_lease_token
            if not token:
                return
            renewed = await asyncio.to_thread(self.room.store.renew_run, self.room.id, token)
            if not renewed:
                self.room.interrupt_event.set()
                self.room.pause_event.set()
                self._set_state(RunState.INTERRUPTED)
                self.emit("agent.error", {"message": "Agent run lost its shared Redis lease and stopped to prevent duplicate execution."})
                return

    def _set_state(self, state: RunState):
        self.room.run_state = state
        self.room.store.set_run_state(self.room.id, state.value)

    def _apply_steering(self, payload: dict):
        if not self.room.run_task or self.room.run_task.done():
            return
        action = payload.get("action")
        if action == "pause":
            self.room.pause_event.clear()
            self._set_state(RunState.PAUSED)
        elif action == "resume":
            self.room.pause_event.set()
            self._set_state(RunState.RUNNING)
        elif action == "interrupt":
            self.room.interrupt_event.set()
            self.room.pause_event.set()
            self._set_state(RunState.INTERRUPTED)
        elif action == "inject" and payload.get("text"):
            self.room.injections.append(str(payload["text"])[:4000])
        elif action == "environment.updated" and payload.get("key"):
            self.room.injections.append(f"Runtime variable {payload['key']} was updated for tools.")

    def _resolve_approval(self, payload: dict):
        gate_id = payload.get("gate_id")
        future = self.room.approval_waiters.get(gate_id)
        if future and not future.done() and payload.get("status") in ("approved", "rejected", "expired"):
            future.set_result(payload.get("status") == "approved")

    async def _request_approval(self, tool: str, args: dict) -> bool:
        gate_id = uuid.uuid4().hex
        requester = self.room.run_requested_by
        active_ids = {member.get("id") for member in self.room.store.list_members(self.room.id)
                      if member.get("id")}
        eligible_voters = sorted(active_ids - {requester})
        self_approval_allowed = False
        # A solo room still requires an explicit human decision, but the only
        # participant can make it. In group rooms the run requester is excluded.
        if not eligible_voters and requester in active_ids:
            eligible_voters = [requester]
            self_approval_allowed = True
        required = min(self.approval_quorum, len(eligible_voters)) if eligible_voters else 1
        gate = self.room.store.create_approval(
            gate_id, self.room.id, requester,
            {"tool": tool, "args": args}, required,
            eligible_voters=eligible_voters,
            requested_quorum=self.approval_quorum,
            self_approval_allowed=self_approval_allowed,
        )
        future = asyncio.get_running_loop().create_future()
        self.room.approval_waiters[gate_id] = future
        self.emit("approval.requested", gate)
        interrupted = asyncio.create_task(self.room.interrupt_event.wait())
        try:
            done, _ = await asyncio.wait(
                {future, interrupted}, timeout=self.approval_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if future in done:
                return bool(future.result()) and not self.room.interrupt_event.is_set()
            gate = self.room.store.expire_approval(gate_id) or {"gate_id": gate_id, "status": "expired"}
            self.emit("approval.updated", gate)
            return False
        finally:
            interrupted.cancel()
            try:
                await interrupted
            except asyncio.CancelledError:
                pass
            self.room.approval_waiters.pop(gate_id, None)

    async def _checkpoint(self, label, memories, state=RunState.RUNNING):
        latest = self.room.store.load(self.room.id)
        if latest.nodes:
            self.room.graph = latest
        snap = Snapshot(step_index=len(self.room.graph.nodes), run_state=state,
                        memory=list(memories), scratch={
                            "planner": self.planner.name,
                            "environment_keys": sorted(self.room.store.get_runtime_env(self.room.id)),
                        })
        node = self.room.graph.add_step(label, stamp(snap))
        self.room.store.save(self.room.graph)
        if self.room.run_lease_token:
            self.room.store.renew_run(self.room.id, self.room.run_lease_token)
        self.emit("state.node_added", {
            "node": asdict(node),
            "snapshot": asdict(self.room.graph.snapshot_for(node.node_id)),
        })


def _expand_env(value, environment):
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z][A-Za-z0-9_]*)\}",
                      lambda match: environment.get(match.group(1), match.group(0)), value)
    if isinstance(value, list):
        return [_expand_env(item, environment) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item, environment) for key, item in value.items()}
    return value


def _redact(value, environment):
    if isinstance(value, str):
        for secret in environment.values():
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(item, environment) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, environment) for key, item in value.items()}
    return value


# Backward-compatible name used by older integrations.
AgentRunner = AIAgentCore
