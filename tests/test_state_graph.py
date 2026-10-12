from backend.state.graph import StateGraph
from backend.state.store import StateStore
from shared.protocols.python.protocols import MemoryEntry, Snapshot


def test_graph_branch_merge_and_sqlite_round_trip(tmp_path):
    store = StateStore(str(tmp_path / "graph.sqlite3"))
    try:
        store.create_room("graph-room", 1.0, "Graph room")
        graph = StateGraph("graph-room")
        root = graph.add_root("Root", Snapshot(memory=[MemoryEntry("user", "task")]))
        left = graph.add_step("Linear branch", Snapshot(memory=[MemoryEntry("assistant", "A")]))
        right = graph.branch(root.node_id, "Alternative", Snapshot(memory=[MemoryEntry("assistant", "B")]))
        merged = graph.merge(left.node_id, right.node_id, "Joined", Snapshot())
        store.save(graph)

        restored = store.load("graph-room")
        assert len(restored.nodes) == 4
        assert restored.head_id == merged.node_id
        assert set(restored.node(merged.node_id).parents) == {left.node_id, right.node_id}
        assert restored.snapshot_for(right.node_id).memory[0].text == "B"
    finally:
        store.close()
