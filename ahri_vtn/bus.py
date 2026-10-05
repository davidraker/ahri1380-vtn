"""
In-process publish/subscribe bus used for live UI updates (SSE) and for conformance scenarios
that wait for VEN behaviour (event responses, report samples, opts).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable, Optional

from .util import utcnow


class LiveBus:
    def __init__(self, history: int = 500):
        self._subscribers: set[asyncio.Queue] = set()
        self.history: deque = deque(maxlen=history)
        self._seq = 0

    def publish(self, topic: str, **data) -> dict:
        self._seq += 1
        event = {"seq": self._seq, "topic": topic, "ts": utcnow().isoformat(), **data}
        self.history.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass    # slow consumer: drop
        return event

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self._subscribers.discard(queue)

    async def wait_for(self, predicate: Callable[[dict], bool], timeout: Optional[float] = None,
                       check: Optional[Callable[[], Awaitable[bool] | bool]] = None) -> Optional[dict]:
        """
        Wait until an event satisfying `predicate` is published (returns it) or the timeout
        expires (returns None). If `check` is given it is evaluated first (and after every
        event) so that conditions that are already true do not block.
        """
        queue = self.subscribe()
        try:
            if check is not None:
                result = check()
                if asyncio.iscoroutine(result):
                    result = await result
                if result:
                    return {"topic": "already", "ts": utcnow().isoformat()}
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                try:
                    event = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    return None
                if predicate(event):
                    return event
                if check is not None:
                    result = check()
                    if asyncio.iscoroutine(result):
                        result = await result
                    if result:
                        return event
        finally:
            self.unsubscribe(queue)
