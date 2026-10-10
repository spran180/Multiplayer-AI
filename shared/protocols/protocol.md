# Wire Protocol Spec — Multiplayer AI Agent

Single transport: one **WebSocket** per room connection. Every frame is an
**Envelope**. Two message families ride the same socket:

1. **RPC** — client sends `rpc`, server replies `rpc.response` (1:1, correlated by `reply_to`/`id`).
2. **Events** — server pushes `agent.*` / `state.*` / `room.*` / `steering.*` to every subscriber in the room (1:N, ordered by per-room `seq`).

Sources of truth, kept in lockstep:
- Python: `shared/protocols/python/protocols.py`
- Swift:  `shared/protocols/swift/Protocols.swift`
- Schema: `shared/protocols/envelope.schema.json`

## Envelope

| field | type | notes |
|---|---|---|
| `v` | int | protocol version, currently `1` |
| `type` | string | discriminator (see tables) |
| `id` | string | unique message id (uuid hex) |
| `room_id` | string | room scope |
| `seq` | int | per-room monotonic sequence (events) |
| `ts` | number | server epoch seconds |
| `sender` | string | `"agent"`, `"system"`, or a user id |
| `reply_to` | string? | for `rpc.response`: the request `id` |
| `correlation_id` | string? | optional caller-side correlation |
| `payload` | object | shape depends on `type` |

## RPC methods

All requests are `type: "rpc"` with `payload = { id, method, args }`.
Responses are `type: "rpc.response"` with `payload = { id, reply_to, ok, result, error }`.

| method | args | result | notes |
|---|---|---|---|
| `room.create` | `{ name?, title? }` | `{ room_id, user, presence, lock }` | creates an empty room + its state graph |
| `room.join` | `{ room_id, user:{id,name,role?} }` | `{ room_id, presence, snapshot, lock }` | attaches you; returns current state |
| `room.leave` | `{}` | `{}` | detaches |
| `room.info` | `{}` | `{ room_id, presence, run_state, node_count }` | |
| `agent.start` | `{ task, provider?, model?, options? }` | `{ run_id, state }` | launches the agent loop |
| `agent.status` | `{}` | `{ run_id, state, current_node_id, step_index }` | |
| `steering.acquire` | `{ user_id }` | `{ lock }` | takes steering rights (unconditional claim for that user) |
| `steering.release` | `{ token }` | `{ lock }` | drops steering rights |
| `steering.handoff` | `{ from, to, token }` | `{ lock }` | atomic transfer to another user |
| `steering.pause` | `{ token }` | `{ state }` | non-blocking pause |
| `steering.resume` | `{ token }` | `{ state }` | resume |
| `steering.interrupt` | `{ token, reason? }` | `{ state }` | stop the run, keep state |
| `steering.inject` | `{ token, text }` | `{ seq, step_index }` | prompt-injection into working memory |
| `steering.lock` | `{}` | `{ lock }` | read current holder/token |
| `state.graph` | `{}` | `{ nodes, edges }` | full graph |
| `state.snapshot` | `{ node_id }` | `{ snapshot }` | snapshot at a node |
| `state.scrub` | `{ node_id }` | `{ snapshot }` | time-travel inspect (alias of snapshot) |
| `state.branch` | `{ from_node_id, token }` | `{ node_id }` | fork from a node (requires lock) |
| `state.merge` | `{ a_node_id, b_node_id, token }` | `{ node_id }` | merge two nodes (requires lock) |

Errors use stable `code`s so clients can branch on them:
`not_found`, `not_room_member`, `missing_token`, `wrong_token`,
`already_held`, `no_active_run`, `invalid_state`, `invalid_args`.

## Event kinds

| type | sender | payload (key fields) |
|---|---|---|
| `agent.started` | agent | `{ run_id, task, state }` |
| `agent.thought` | agent | `{ step_index, text }` |
| `agent.tool_call` | agent | `{ step_index, name, args }` |
| `agent.tool_result` | agent | `{ step_index, name, ok, result, duration_ms }` |
| `agent.step` | agent | `{ node_id, step_index, snapshot_id }` |
| `agent.done` | agent | `{ state, final_node_id, summary? }` |
| `agent.error` | agent | `{ message, state }` |
| `state.node_added` | system | `{ node, snapshot? }` |
| `state.snapshot` | system | `{ node_id, snapshot }` |
| `state.branch` | system | `{ node, from_node_id }` |
| `state.merge` | system | `{ node, a_node_id, b_node_id }` |
| `room.presence` | system | `{ presence }` |
| `steering.status` | system | `{ state, action, actor }` |
| `steering.lock_changed` | system | `{ lock }` |

## Steering lock lifecycle

Exactly **one holder** at a time. `token` is a rotating credential:

- `acquire(user)` → holder = user, token = T1. (Re-acquire by the same user keeps T1.)
- `handoff(from, to, token=T1)` → succeeds only if current token == T1 and holder == from; then holder = to, token = T2 (new).
- `pause/resume/interrupt/inject` all require the **current** token.
- `release(token)` → holder = null, token rotates to T3.

Hand-off and release are atomic compare-and-swap on the token, so two clients
cannot both hold steering rights even if their messages arrive interleaved.
