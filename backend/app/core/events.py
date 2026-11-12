"""In-process event bus. Components publish typed events; observers (monitoring,
websockets, audit) subscribe without coupling to the producers."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..domain.models import utcnow


@dataclass(frozen=True)
class Event:
    type: str  # bar | signal | risk_decision | order | fill | approval | alert
    payload: dict[str, Any]
    ts: datetime = field(default_factory=utcnow)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Subscribe to one event type, or "*" for all."""
        self._handlers[event_type].append(handler)

    def publish(self, event_type: str, payload: dict[str, Any]) -> Event:
        event = Event(event_type, payload)
        for handler in (*self._handlers[event_type], *self._handlers["*"]):
            handler(event)  # a failing observer must be loud, not silent
        return event
