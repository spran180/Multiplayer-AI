"""FastAPI application: room REST API, WebSocket RPC, streaming, and steering."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.agent.core import AIAgentCore
from backend.auth import AuthError, AuthStore
from backend.agent.llm.groq import GroqPlanner
from backend.agent.llm.mock import MockPlanner
from backend.agent.llm.ollama import OllamaPlanner
from backend.config import settings
from backend.realtime.bus import PubSubBus, RedisPubSubBus
from backend.state.graph import GraphError, StateGraph
from backend.state.snapshot import stamp
from backend.state.store import RedisStateStore, StateStore
from backend.steering.lock import LockManager, RedisLockManager, SteeringError
from shared.protocols.python.protocols import Envelope, MessageKind, RunState, Snapshot, User


@dataclass
class Room:
    id: str
    name: str
    created_at: float = field(default_factory=time.time)
    graph: StateGraph = field(init=False)
    lock: Any = field(init=False)
    store: Any = field(init=False, repr=False)
    bus: Any = field(init=False, repr=False)
    run_state: RunState = RunState.IDLE
    pause_event: asyncio.Event | None = None
    interrupt_event: asyncio.Event | None = None
    injections: list[str] = field(default_factory=list)
    run_task: asyncio.Task | None = None
    run_lease_token: str | None = None
    run_requested_by: str = ""
    approval_waiters: dict[str, asyncio.Future] = field(default_factory=dict)
    runtime_env: dict[str, str] = field(default_factory=dict)
    members: dict[str, dict] = field(default_factory=dict)
    presence_signature: tuple[str, ...] = ()
    sockets: set[WebSocket] = field(default_factory=set)
    subscribers: dict[WebSocket, Any] = field(default_factory=dict)
    outboxes: dict[WebSocket, asyncio.Queue] = field(default_factory=dict)
    writers: dict[WebSocket, asyncio.Task] = field(default_factory=dict)

    def __post_init__(self):
        self.graph = StateGraph(self.id)
        self.store = store
        self.bus = bus
        self.lock = RedisLockManager(self.id, store.client) if settings.redis_url else LockManager(self.id)


if settings.redis_url:
    store = RedisStateStore(settings.redis_url)
    bus = RedisPubSubBus(settings.redis_url)
    auth_store = AuthStore(settings.redis_url, settings.database_path)
else:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(settings.database_path)
    bus = PubSubBus()
    auth_store = AuthStore(sqlite_path=settings.database_path)
rooms: dict[str, Room] = {}


def _room_info(room: Room) -> dict:
    lock = _safe_lock(room)
    return {"id": room.id, "name": room.name, "created_at": room.created_at,
            "run_state": _run_state(room), "members": _unique_members(room.id),
            "approval_quorum": settings.approval_quorum,
            "steering_lock": lock, "graph": store.load(room.id).to_dict(),
            "approvals": store.list_approvals(room.id)}


def _unique_members(room_id: str) -> list[dict]:
    members = {}
    for member in store.list_members(room_id):
        members[member.get("id")] = {key: value for key, value in member.items()
                                     if key != "connection_id"}
    return list(members.values())


def _invite_url(room_id: str, user_id: str) -> str:
    token = auth_store.create_invite(room_id, user_id)
    return f"{settings.public_base_url}/r/{room_id}?invite={token}"


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)


class Registration(Credentials):
    name: str = Field(min_length=1, max_length=80)


class RoomCreation(BaseModel):
    name: str = Field(default="Shared agent room", max_length=100)


def require_user(request: Request) -> dict:
    user = auth_store.get_session_user(request.cookies.get("multiplayer_session"))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user


def set_session_cookie(response: Response, token: str):
    response.set_cookie("multiplayer_session", token, max_age=30 * 86400,
                        httponly=True, secure=settings.public_base_url.startswith("https://"),
                        samesite="lax", path="/")


def _check_auth_rate_limit(request: Request, action: str, email: str) -> None:
    remote_ip = request.client.host if request.client else "unknown"
    if not auth_store.consume_auth_rate_limit(action, email, remote_ip):
        raise HTTPException(429, "Too many authentication attempts. Wait a few minutes and try again.")


def _safe_lock(room: Room) -> dict:
    lock = asdict(room.lock.current())
    lock.pop("token", None)
    return lock


def _run_state(room: Room) -> str:
    return store.get_run_state(room.id) or room.run_state.value


def _snapshot_output(snapshot: Snapshot) -> str:
    value = snapshot.scratch.get("merged_output")
    if value:
        return str(value)
    for entry in reversed(snapshot.memory):
        if entry.kind == "assistant":
            return entry.text
    return "\n".join(entry.text for entry in snapshot.memory[-8:])


def _create_planner():
    provider = settings.llm_provider
    if provider == "auto":
        provider = "groq" if settings.groq_api_key else "mock"
    if provider == "groq":
        return GroqPlanner(settings.groq_api_key, settings.groq_model)
    if provider == "ollama":
        return OllamaPlanner(settings.ollama_base_url, settings.ollama_model,
                             settings.ollama_timeout_seconds)
    if provider == "mock":
        return MockPlanner()
    raise ValueError("LLM_PROVIDER must be auto, groq, ollama, or mock")


def _create_room(name: str = "Shared agent room", room_id: str | None = None) -> Room:
    rid = room_id or uuid.uuid4().hex
    if rid in rooms:
        raise ValueError("room already exists")
    room = Room(rid, name.strip()[:100] or "Shared agent room")
    room.graph = store.load(rid)
    store.create_room(rid, room.created_at, room.name)
    store.set_run_state(rid, RunState.IDLE.value)
    rooms[rid] = room
    return room


def _restore_room(room_id: str) -> Room:
    existing = rooms.get(room_id)
    if existing:
        return existing
    metadata = store.room_metadata(room_id)
    if not metadata:
        raise KeyError(room_id)
    room = Room(room_id, metadata.get("name", "Restored room"),
                created_at=float(metadata.get("created_at", time.time())))
    room.graph = store.load(room_id)
    rooms[room_id] = room
    return room


for _room_id in store.list_rooms():
    _restore_room(_room_id)

app = FastAPI(title="Multiplayer AI Agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=False)
app.mount("/app", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "frontend" / "web"),
                               html=True), name="room-web")


def _init_room_controls(room: Room):
    room.pause_event = asyncio.Event()
    room.pause_event.set()
    room.interrupt_event = asyncio.Event()


@app.on_event("startup")
async def initialize_rooms():
    if settings.llm_provider not in {"auto", "groq", "ollama", "mock"}:
        raise RuntimeError("LLM_PROVIDER must be auto, groq, ollama, or mock")
    if not 1 <= settings.max_agent_steps <= 100:
        raise RuntimeError("MAX_AGENT_STEPS must be between 1 and 100")
    if not 1 <= settings.approval_quorum <= 50:
        raise RuntimeError("APPROVAL_QUORUM must be between 1 and 50")
    if settings.app_env == "production":
        if not settings.redis_url:
            raise RuntimeError("REDIS_URL is required when APP_ENV=production")
        if not settings.public_base_url.startswith("https://"):
            raise RuntimeError("PUBLIC_BASE_URL must use HTTPS in production")
        effective_provider = ("groq" if settings.groq_api_key else "mock") if settings.llm_provider == "auto" else settings.llm_provider
        if effective_provider == "mock":
            raise RuntimeError("A production deployment must configure a real LLM provider")
        if effective_provider == "groq" and not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
        if effective_provider == "ollama" and settings.ollama_base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise RuntimeError("Configure a remotely reachable OLLAMA_BASE_URL in production")
    for room in rooms.values():
        _init_room_controls(room)


@app.on_event("shutdown")
async def close_services():
    active_runs = []
    for room in rooms.values():
        task = room.run_task
        if task and not task.done():
            if room.interrupt_event:
                room.interrupt_event.set()
            if room.pause_event:
                room.pause_event.set()
            task.cancel()
            active_runs.append(task)
    if active_runs:
        try:
            await asyncio.wait_for(asyncio.gather(*active_runs, return_exceptions=True), timeout=10)
        except asyncio.TimeoutError:
            pass
    if hasattr(bus, "close"):
        bus.close()
    store.close()
    auth_store.close()


async def broadcast(room: Room, kind: str, payload: dict, sender: str = "agent"):
    try:
        message_kind = MessageKind(kind)
    except ValueError:
        return
    env = Envelope.event(message_kind, room.id, payload, sender=sender)
    data = json.dumps(env.to_dict(), default=_json_default)
    room.bus.publish(f"room:{room.id}", data)


async def notify_user(room: Room, user_id: str, kind: str, payload: dict):
    try:
        message_kind = MessageKind(kind)
    except ValueError:
        return
    env = Envelope.event(message_kind, room.id, payload, sender="server")
    room.bus.publish(f"room:{room.id}:user:{user_id}", json.dumps(env.to_dict(), default=_json_default))


async def _refresh_presence(room: Room, sender: str = "server"):
    members = _unique_members(room.id)
    signature = tuple(sorted(member.get("id", "") for member in members))
    if signature == room.presence_signature:
        return
    room.presence_signature = signature
    await broadcast(room, "room.presence", {"members": members}, sender=sender)
    for gate in store.reconcile_approvals(room.id, signature):
        await broadcast(room, "approval.updated", gate, sender="server")


async def _socket_writer(ws: WebSocket, outbox: asyncio.Queue):
    try:
        while True:
            data = await outbox.get()
            if data is None:
                return
            await ws.send_text(data)
    except Exception:
        return


def _json_default(value):
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "__dict__"):
        return value.__dict__
    raise TypeError(f"cannot encode {type(value).__name__}")


@app.get("/health")
def health():
    return {"ok": True, "model": settings.llm_provider,
            "coordination": "redis" if settings.redis_url else "single-instance"}


@app.get("/ready")
def ready():
    """Readiness probe: verify every configured shared state dependency."""
    try:
        store.ping()
        auth_store.ping()
        if hasattr(bus, "ping"):
            bus.ping()
    except Exception as exc:
        raise HTTPException(503, "A configured persistence or coordination service is unavailable.") from exc
    return {"ok": True, "coordination": "redis" if settings.redis_url else "single-instance"}


@app.post("/auth/register", status_code=201)
def register_account(body: Registration, request: Request, response: Response):
    _check_auth_rate_limit(request, "register", body.email)
    try:
        user = auth_store.register(body.email, body.password, body.name)
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    set_session_cookie(response, auth_store.create_session(user["id"]))
    return {"user": user}


@app.post("/auth/login")
def login(body: Credentials, request: Request, response: Response):
    _check_auth_rate_limit(request, "login", body.email)
    user = auth_store.authenticate(body.email, body.password)
    if not user:
        raise HTTPException(401, "Email or password is incorrect.")
    set_session_cookie(response, auth_store.create_session(user["id"]))
    return {"user": user}


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    auth_store.revoke_session(request.cookies.get("multiplayer_session"))
    response.delete_cookie("multiplayer_session", path="/", httponly=True,
                           secure=settings.public_base_url.startswith("https://"), samesite="lax")
    return {"ok": True}


@app.get("/auth/me")
def who_am_i(user: dict = Depends(require_user)):
    return {"user": user}


@app.get("/rooms")
def list_rooms(user: dict = Depends(require_user)):
    result = []
    for room_id in auth_store.room_ids_for_user(user["id"]):
        try:
            room = _restore_room(room_id)
        except KeyError:
            continue
        result.append({"id": room.id, "name": room.name, "run_state": _run_state(room),
                       "member_count": len(_unique_members(room.id))})
    return result


@app.post("/rooms")
async def create_room(body: Optional[RoomCreation] = None, user: dict = Depends(require_user)):
    try:
        room = _create_room(body.name if body else "Shared agent room")
        invite = auth_store.add_room_owner(room.id, user["id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _init_room_controls(room)
    return {**_room_info(room),
            "share_url": f"{settings.public_base_url}/r/{room.id}?invite={invite}"}


@app.get("/rooms/{room_id}")
async def get_room(room_id: str, invite: str = Query(default=""), user: dict = Depends(require_user)):
    try:
        room = _restore_room(room_id)
    except KeyError:
        raise HTTPException(404, "room not found")
    if not auth_store.has_members(room_id):
        auth_store.claim_legacy_room(room_id, user["id"])
    if not auth_store.authorize_room(room_id, user["id"], invite):
        raise HTTPException(403, "You do not have access to this room. Open a valid room invitation.")
    if room.pause_event is None:
        _init_room_controls(room)
    return {**_room_info(room), "share_url": _invite_url(room.id, user["id"])}


@app.get("/r/{room_id}", include_in_schema=False)
async def room_invite(room_id: str, request: Request, invite: str = Query(default="")):
    try:
        room = _restore_room(room_id)
    except KeyError:
        raise HTTPException(404, "room not found")
    user = auth_store.get_session_user(request.cookies.get("multiplayer_session"))
    query = f"room={room.id}" + (f"&invite={invite}" if invite else "")
    if not user:
        return RedirectResponse(url=f"/app/?{query}")
    if not auth_store.has_members(room.id):
        auth_store.claim_legacy_room(room.id, user["id"])
    if not auth_store.authorize_room(room.id, user["id"], invite):
        return RedirectResponse(url=f"/app/?{query}")
    return RedirectResponse(url=f"/app/?room={room.id}")


@app.post("/rooms/{room_id}/invite")
async def create_room_invite(room_id: str, user: dict = Depends(require_user)):
    try:
        _restore_room(room_id)
    except KeyError:
        raise HTTPException(404, "room not found")
    if not auth_store.authorize_room(room_id, user["id"]):
        raise HTTPException(403, "You do not have access to this room.")
    return {"share_url": _invite_url(room_id, user["id"])}


@app.websocket("/ws/{room_id}")
async def room_socket(ws: WebSocket, room_id: str):
    try:
        room = _restore_room(room_id)
    except KeyError:
        await ws.close(code=4404, reason="room not found")
        return
    user = auth_store.get_session_user(ws.cookies.get("multiplayer_session"))
    if not user:
        await ws.close(code=4401, reason="authentication required")
        return
    origin = ws.headers.get("origin")
    host = ws.headers.get("host", "").lower()
    if origin and urlsplit(origin).netloc.lower() != host:
        await ws.close(code=4403, reason="cross-origin websocket rejected")
        return
    if not auth_store.authorize_room(room_id, user["id"]):
        await ws.close(code=4403, reason="room access denied")
        return
    if room.pause_event is None:
        _init_room_controls(room)
    await ws.accept()
    uid = user["id"]
    connection_id = uuid.uuid4().hex
    participant = User(id=uid, name=user["name"])
    room.sockets.add(ws)
    outbox: asyncio.Queue = asyncio.Queue(maxsize=256)
    room.outboxes[ws] = outbox
    room.writers[ws] = asyncio.create_task(_socket_writer(ws, outbox))
    loop = asyncio.get_running_loop()

    def enqueue(data):
        try:
            outbox.put_nowait(data)
        except asyncio.QueueFull:
            # A slow consumer must not retain unbounded room-event history.
            asyncio.create_task(ws.close(code=1013, reason="client is too slow"))

    def subscriber(data):
        encoded = data if isinstance(data, str) else json.dumps(data, default=_json_default)
        loop.call_soon_threadsafe(enqueue, encoded)
    room.subscribers[ws] = subscriber
    room.bus.subscribe(f"room:{room.id}", subscriber)
    private_subscriber = subscriber
    private_topic = f"room:{room.id}:user:{uid}"
    room.bus.subscribe(private_topic, private_subscriber)
    member = {"id": uid, "name": participant.name, "connection_id": connection_id,
              "connected_at": time.time()}
    room.members[connection_id] = member
    store.add_member(room.id, member)
    await _refresh_presence(room, sender=uid)
    room.graph = store.load(room.id)
    enqueue(json.dumps(Envelope.event(MessageKind.STATE_SNAPSHOT, room.id,
        {"graph": room.graph.to_dict(), "run_state": _run_state(room),
         "approvals": store.list_approvals(room.id)}, sender="server").to_dict(), default=_json_default))
    lock_view = asdict(room.lock.current())
    token = lock_view.pop("token")
    enqueue(json.dumps(Envelope.event(MessageKind.STEERING_LOCK_CHANGED, room.id,
        {"lock": lock_view, "your_token": token if room.lock.current().holder_id == uid else None},
        sender="server").to_dict(), default=_json_default))
    try:
        while True:
            raw_frame = await ws.receive_text()
            if len(raw_frame.encode("utf-8")) > 65536:
                await ws.close(code=1009, reason="message too large")
                break
            frame = json.loads(raw_frame)
            if not isinstance(frame, dict):
                continue
            if frame.get("type") != MessageKind.RPC.value:
                continue
            request = frame.get("payload") or {}
            if not isinstance(request, dict):
                continue
            request_id = request.get("id", "")
            args = request.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            try:
                if request.get("method") == "ping":
                    store.add_member(room.id, {**member, "connected_at": time.time()})
                    held = room.lock.current()
                    if held.holder_id == uid:
                        room.lock.renew(held.token, uid)
                    await _refresh_presence(room)
                    result = {"ok": True, "ts": time.time()}
                else:
                    result = await dispatch(room, uid, participant.name, request.get("method", ""),
                                            args)
                payload = {"reply_to": request_id, "ok": True, "result": result}
            except (ValueError, KeyError, SteeringError, GraphError) as exc:
                payload = {"reply_to": request_id, "ok": False,
                           "error": {"code": getattr(exc, "code", "invalid_request"), "message": str(exc)}}
            response = Envelope.event(MessageKind.RPC_RESPONSE, room.id, payload,
                                      sender="server", seq=frame.get("seq", 0))
            enqueue(json.dumps(response.to_dict(), default=_json_default))
    except WebSocketDisconnect:
        pass
    except (ValueError, json.JSONDecodeError):
        pass
    finally:
        room.sockets.discard(ws)
        subscriber = room.subscribers.pop(ws, None)
        if subscriber:
            room.bus.unsubscribe(f"room:{room.id}", subscriber)
            room.bus.unsubscribe(private_topic, subscriber)
        try:
            outbox.put_nowait(None)
        except asyncio.QueueFull:
            pass
        writer = room.writers.pop(ws, None)
        if writer and not writer.done():
            writer.cancel()
        room.outboxes.pop(ws, None)
        room.members.pop(connection_id, None)
        store.remove_member(room.id, connection_id)
        current_lock = room.lock.current()
        if current_lock.holder_id == uid and not any(member.get("id") == uid for member in _unique_members(room.id)):
            room.lock.release(current_lock.token)
            await broadcast(room, "steering.lock_changed", {"lock": _safe_lock(room)}, sender=uid)
        await _refresh_presence(room, sender=uid)


async def dispatch(room: Room, uid: str, name: str, method: str, args: dict) -> Any:
    if method == "room.info":
        return _room_info(room)
    if method == "steering.acquire":
        lock = asdict(room.lock.acquire(uid, name))
        await broadcast(room, "steering.lock_changed", {"lock": {k: v for k, v in lock.items() if k != "token"}})
        return {"lock": lock}
    if method == "steering.release":
        lock = asdict(room.lock.release(str(args.get("token", ""))))
        await broadcast(room, "steering.lock_changed", {"lock": {k: v for k, v in lock.items() if k != "token"}})
        return {"lock": {k: v for k, v in lock.items() if k != "token"}}
    if method == "steering.handoff":
        target_id = str(args.get("target_id", ""))
        target = next((m for m in _unique_members(room.id) if m.get("id") == target_id), None)
        if target is None:
            raise ValueError("handoff target must be connected to this room")
        lock = asdict(room.lock.handoff(str(args.get("token", "")), uid,
                                        target_id, target.get("name")))
        await broadcast(room, "steering.lock_changed", {"lock": {k: v for k, v in lock.items() if k != "token"}})
        await notify_user(room, target_id, "steering.lock_changed",
                          {"lock": {k: v for k, v in lock.items() if k != "token"},
                           "your_token": lock["token"]})
        return {"lock": {k: v for k, v in lock.items() if k != "token"}}
    if method in ("steering.pause", "steering.resume", "steering.interrupt", "steering.inject"):
        token = str(args.get("token", ""))
        if not room.lock.holds(token, uid):
            raise SteeringError("wrong_token", "current steering token and holder are required")
        if method == "steering.pause":
            assert room.pause_event is not None
            room.run_state = RunState.PAUSED
            detail = {"action": "pause", "state": room.run_state.value, "actor": uid}
        elif method == "steering.resume":
            assert room.pause_event is not None
            room.run_state = RunState.RUNNING if room.run_task else RunState.IDLE
            detail = {"action": "resume", "state": room.run_state.value, "actor": uid}
        elif method == "steering.interrupt":
            assert room.interrupt_event is not None and room.pause_event is not None
            room.run_state = RunState.INTERRUPTED
            detail = {"action": "interrupt", "state": room.run_state.value, "actor": uid}
        else:
            text = str(args.get("text", "")).strip()
            if not text:
                raise ValueError("injection text is required")
            detail = {"action": "inject", "text": text[:4000], "actor": uid, "state": room.run_state.value}
        if not store.run_is_active(room.id):
            raise ValueError("steering requires an active agent run")
        store.set_run_state(room.id, room.run_state.value)
        await broadcast(room, "steering.status", detail, sender=uid)
        return {"state": room.run_state.value}
    if method == "steering.set_env":
        token = str(args.get("token", ""))
        if not room.lock.holds(token, uid):
            raise SteeringError("wrong_token", "current steering token and holder are required")
        key = str(args.get("key", "")).strip()
        value = str(args.get("value", ""))
        if not key or not key.replace("_", "").isalnum() or not key[0].isalpha():
            raise ValueError("environment variable names must be alphanumeric with underscores")
        if len(key) > 80 or len(value) > 8000:
            raise ValueError("environment variable exceeds the allowed size")
        if not store.run_is_active(room.id):
            raise ValueError("environment updates require an active agent run")
        room.runtime_env[key] = value
        store.set_runtime_env(room.id, key, value)
        await broadcast(room, "steering.status", {"action": "environment.updated", "key": key,
                                                   "actor": uid}, sender=uid)
        return {"updated": True, "key": key}
    if method == "approval.vote":
        gate_id = str(args.get("gate_id", ""))
        approve = args.get("approve")
        if not isinstance(approve, bool):
            raise ValueError("approval vote must be true or false")
        gate = store.cast_approval_vote(gate_id, uid, name, approve,
                                        str(args.get("reason", "")),
                                        active_voters=[member.get("id") for member in _unique_members(room.id)])
        await broadcast(room, "approval.updated", gate, sender=uid)
        return gate
    if method == "agent.start":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task is required")
        if len(task) > 12000:
            raise ValueError("task must be 12,000 characters or fewer")
        if room.run_task and not room.run_task.done():
            raise ValueError("an agent run is already active")
        # Construct the provider before acquiring the shared lease. A missing
        # API key or invalid provider must not leave the room locked until TTL.
        planner = _create_planner()
        assert room.interrupt_event is not None and room.pause_event is not None
        room.graph = store.load(room.id)
        lease = store.acquire_run(room.id)
        if lease is None:
            raise ValueError("an agent run is already active on another server")
        room.interrupt_event.clear()
        room.pause_event.set()
        room.run_lease_token = lease
        room.run_requested_by = uid
        try:
            runner = AIAgentCore(
                room,
                planner,
                lambda kind, payload: asyncio.create_task(broadcast(room, kind, payload)),
                settings.max_agent_steps,
                settings.approval_quorum,
                settings.approval_timeout_seconds,
            )
            room.run_task = asyncio.create_task(runner.run(task))
        except Exception:
            store.release_run(room.id, lease)
            room.run_lease_token = None
            room.run_requested_by = ""
            room.run_task = None
            raise
        return {"started": True, "model": planner.name}
    if method == "agent.status":
        return {"state": _run_state(room), "running": store.run_is_active(room.id)}
    if method == "state.scrub":
        room.graph = store.load(room.id)
        node_id = str(args.get("node_id", ""))
        return {"node": room.graph._node_to_dict(room.graph.node(node_id)),
                "snapshot": room.graph._snapshot_to_dict(room.graph.snapshot_for(node_id))}
    if method == "state.branch":
        room.graph = store.load(room.id)
        node_id = str(args.get("node_id", room.graph.head_id or ""))
        origin = room.graph.snapshot_for(node_id)
        snap = Snapshot(step_index=origin.step_index, run_state=room.run_state,
                        memory=list(origin.memory), scratch=dict(origin.scratch))
        node = room.graph.branch(node_id, str(args.get("name", "Hypothesis"))[:100], stamp(snap))
        store.save(room.graph)
        await broadcast(room, "state.branch", {"node": asdict(node)})
        return {"node": asdict(node), "graph": room.graph.to_dict()}
    if method == "state.merge":
        room.graph = store.load(room.id)
        a, b = str(args.get("a", "")), str(args.get("b", ""))
        left, right = room.graph.snapshot_for(a), room.graph.snapshot_for(b)
        planner = _create_planner()
        left_output, right_output = _snapshot_output(left), _snapshot_output(right)
        merged_output = await asyncio.to_thread(planner.merge_outputs, left_output, right_output)
        merged_memory = list({(entry.kind, entry.text): entry for entry in left.memory + right.memory}.values())
        snap = Snapshot(step_index=max(left.step_index, right.step_index) + 1,
                        run_state=RunState(_run_state(room)), memory=merged_memory,
                        scratch={"merged_from": [a, b], "merged_output": merged_output})
        node = room.graph.merge(a, b, str(args.get("name", "Merged hypothesis"))[:100], stamp(snap))
        store.save(room.graph)
        await broadcast(room, "state.merge", {"node": asdict(node)})
        return {"node": asdict(node), "graph": room.graph.to_dict()}
    if method == "state.graph":
        room.graph = store.load(room.id)
        return room.graph.to_dict()
    raise ValueError(f"unknown RPC method: {method}")
