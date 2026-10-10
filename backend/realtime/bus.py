"""In-process pub/sub bus — the fan-out abstraction.

Design intent: this is the SEAM for going distributed. The rest of the code
only calls ``subscribe(topic, handler)`` / ``publish(topic, payload)`` and
never cares how fan-out is delivered. Today that's an in-memory topic map;
tomorrow it's Redis Pub/Sub across many backend nodes serving the same room.

Topic convention: ``room:<room_id>`` carries the JSON string for one envelope.
Handlers run synchronously on the publisher's thread, so handlers must be
fast and thread-safe (they are: each just enqueues onto a connection queue).
"""

from __future__ import annotations

import threading
import json
from typing import Any, Callable, Dict, List


class PubSubBus:
    def __init__(self):
        self._subs: Dict[str, List[Callable[[Any], None]]] = {}
        self._mutex = threading.Lock()

    def subscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        with self._mutex:
            self._subs.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        with self._mutex:
            handlers = self._subs.get(topic)
            if handlers and handler in handlers:
                handlers.remove(handler)
            if handlers is not None and not handlers:
                self._subs.pop(topic, None)

    def publish(self, topic: str, payload: Any) -> int:
        """Deliver to all subscribers of ``topic``. Returns fan-out count."""
        with self._mutex:
            handlers = list(self._subs.get(topic, ()))
        for h in handlers:
            try:
                h(payload)
            except Exception:
                # A broken subscriber must never take down the publisher or
                # the other subscribers in the fan-out.
                pass
        return len(handlers)

    def topic_count(self, topic: str) -> int:
        with self._mutex:
            return len(self._subs.get(topic, ()))


class RedisPubSubBus(PubSubBus):
    """Redis Pub/Sub fan-out across API replicas.

    Each topic has one listener thread per process. All handlers still run
    through the same subscribe/publish interface as the local bus.
    """

    def __init__(self, url: str = "redis://localhost:6379/0", **kwargs):
        super().__init__()
        import redis
        self._redis = redis.Redis.from_url(url, decode_responses=True, **kwargs)
        self._redis.ping()
        self._topic_lock = threading.RLock()
        self._topic_workers: Dict[str, tuple[Any, threading.Thread, threading.Event]] = {}

    def subscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        with self._topic_lock:
            self._subs.setdefault(topic, []).append(handler)
            if topic in self._topic_workers:
                return
            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(topic)
            stop = threading.Event()
            worker = threading.Thread(target=self._listen, args=(topic, pubsub, stop),
                                      name=f"redis-sub-{topic}", daemon=True)
            self._topic_workers[topic] = (pubsub, worker, stop)
            worker.start()

    def _listen(self, topic: str, pubsub, stop: threading.Event) -> None:
        try:
            for message in pubsub.listen():
                if stop.is_set():
                    return
                if message.get("type") != "message":
                    continue
                payload = message.get("data")
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    pass
                with self._topic_lock:
                    handlers = list(self._subs.get(topic, ()))
                for handler in handlers:
                    try:
                        handler(payload)
                    except Exception:
                        continue
        except Exception:
            if not stop.is_set():
                return

    def unsubscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        with self._topic_lock:
            handlers = self._subs.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)
            if handlers:
                return
            self._subs.pop(topic, None)
            worker_info = self._topic_workers.pop(topic, None)
        if worker_info:
            pubsub, worker, stop = worker_info
            stop.set()
            try:
                pubsub.close()
            except Exception:
                pass
            if worker is not threading.current_thread():
                worker.join(timeout=1.0)

    def publish(self, topic: str, payload: Any) -> int:
        value = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        return int(self._redis.publish(topic, value))

    def close(self) -> None:
        with self._topic_lock:
            topics = list(self._topic_workers)
        for topic in topics:
            with self._topic_lock:
                handlers = list(self._subs.get(topic, ()))
            for handler in handlers:
                self.unsubscribe(topic, handler)
        self._redis.close()
