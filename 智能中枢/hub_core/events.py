from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List


class EventSubscription:
    def __init__(self, event_queue: "queue.Queue[Dict[str, Any]]"):
        self.queue = event_queue


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []

    def subscribe(self) -> EventSubscription:
        event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(event_queue)
        return EventSubscription(event_queue)

    def unsubscribe(self, subscription: EventSubscription) -> None:
        with self._lock:
            if subscription.queue in self._subscribers:
                self._subscribers.remove(subscription.queue)

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for event_queue in subscribers:
            try:
                event_queue.put_nowait(event)
            except queue.Full:
                try:
                    event_queue.get_nowait()
                    event_queue.put_nowait(event)
                except queue.Empty:
                    pass
