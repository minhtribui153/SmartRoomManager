from __future__ import annotations

"""
Room-level event bus.

This is a higher-level pub/sub layer on top of the low-level Arduino
serial event bus. It carries semantic events such as sensor snapshots,
session state changes, and link status.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List


class RoomEventType(str, Enum):
    SENSOR_SNAPSHOT = "sensor_snapshot"
    SESSION_CHANGED = "session_changed"
    LINK_STATE = "link_state"
    AUTH = "auth"
    LOG = "log"


@dataclass(frozen=True)
class RoomEvent:
    type: RoomEventType
    payload: Dict[str, Any]


RoomSubscriber = Callable[[RoomEvent], None]


class RoomEventBus:
    def __init__(self) -> None:
        self._subs: List[RoomSubscriber] = []

    def subscribe(self, fn: RoomSubscriber) -> Callable[[], None]:
        self._subs.append(fn)

        def unsubscribe() -> None:
            try:
                self._subs.remove(fn)
            except ValueError:
                pass

        return unsubscribe

    def publish(self, ev: RoomEvent) -> None:
        # Snapshot to avoid modification during iteration
        subs = list(self._subs)
        for fn in subs:
            try:
                fn(ev)
            except Exception:
                # Do not let subscriber failures kill the bus
                pass

