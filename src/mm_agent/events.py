"""In-process async event bus with bounded subscribers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .domain import utcnow
from .util import redact


@dataclass(frozen=True)
class Event:
    topic: str
    payload: dict[str, Any]
    at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "payload": redact(self.payload), "at": self.at.isoformat()}


class EventBus:
    def __init__(self, history_size: int = 500) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: deque[Event] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    @property
    def history(self) -> list[Event]:
        return list(self._history)

    async def publish(self, topic: str, **payload: Any) -> Event:
        event = Event(topic=topic, payload=redact(payload))
        async with self._lock:
            self._history.append(event)
            queues = list(self._subscribers)
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return event

    @asynccontextmanager
    async def subscribe(self, maxsize: int = 200) -> AsyncIterator[AsyncIterator[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)

        async def iterator() -> AsyncIterator[Event]:
            while True:
                yield await queue.get()

        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield iterator()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
