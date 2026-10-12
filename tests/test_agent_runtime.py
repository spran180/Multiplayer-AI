import asyncio
from types import SimpleNamespace

from backend.agent.core import AIAgentCore
from backend.agent.llm.mock import MockPlanner
from backend.realtime.bus import PubSubBus
from backend.state.graph import StateGraph
from backend.state.store import StateStore
from shared.protocols.python.protocols import RunState


def test_agent_executes_tools_and_persists_checkpoints(tmp_path):
    async def run_agent():
        store = StateStore(str(tmp_path / "state.sqlite3"))
        room_id = "runtime-room"
        store.create_room(room_id, 1.0)
        room = SimpleNamespace(
            id=room_id,
            store=store,
            bus=PubSubBus(),
            graph=StateGraph(room_id),
            pause_event=asyncio.Event(),
            interrupt_event=asyncio.Event(),
            injections=[],
            approval_waiters={},
            run_requested_by="user-1",
            run_lease_token=store.acquire_run(room_id),
            run_task=None,
            run_state=RunState.IDLE,
        )
        room.pause_event.set()
        events = []
        runner = AIAgentCore(room, MockPlanner(horizon=4),
                             lambda kind, payload: events.append((kind, payload)), max_steps=6)
        try:
            await runner.run("calculate a verified result")
            assert store.get_run_state(room_id) == RunState.FINISHED.value
            assert not store.run_is_active(room_id)
            assert any(kind == "agent.tool_result" and payload["tool"] == "calculate"
                       for kind, payload in events)
            assert any(kind == "agent.tool_result" and payload["tool"] == "verify"
                       for kind, payload in events)
            graph = store.load(room_id)
            assert len(graph.nodes) >= 5
            assert graph.head_id is not None
        finally:
            store.close()

    asyncio.run(run_agent())
