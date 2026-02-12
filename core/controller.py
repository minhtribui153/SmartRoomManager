from __future__ import annotations

"""
RoomController
--------------

Core state machine for a single room:
- Tracks the current and upcoming sessions from the database.
- Listens to Arduino serial events (via ArduinoSerialClient's EventBus).
- Publishes high-level room events via RoomEventBus.
- Drives the Arduino UI via an injected adapter (arduino argument).
"""

import asyncio
from datetime import datetime
from typing import Any, Optional

from config import RoomConfig
from core.bus import RoomEventBus, RoomEvent, RoomEventType
from storage.repo import Repo, Session


class RoomController:
    def __init__(
        self,
        room: RoomConfig,
        repo: Repo,
        arduino: Any,
        bus: RoomEventBus,
        serial_bus: Optional[Any] = None,
    ) -> None:
        self._room = room
        self._repo = repo
        self._arduino = arduino
        self._bus = bus
        self._serial_bus = serial_bus

        self._current_session: Optional[Session] = None
        self._running = False

        # Subscribe to low-level serial events if provided
        if self._serial_bus is not None:
            self._serial_bus.subscribe(self._on_serial_event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        """Main controller loop."""
        self._running = True
        tick_s = self._room.controller_tick_ms / 1000.0

        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(tick_s)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def authenticate_user_hash(self, user_hash: str) -> bool:
        """
        Socket server entrypoint: authenticate a user by hash only.

        The organisation ensures that user hashes in the DB are correct and
        linked to sessions; here we only validate the hash against the DB.
        """
        now = datetime.now()
        user = self._repo.get_user_by_hash(user_hash, now=now)
        if user is None:
            self._bus.publish(
                RoomEvent(
                    type=RoomEventType.AUTH,
                    payload={"ok": False, "reason": "invalid_hash"},
                )
            )
            return False

        self._bus.publish(
            RoomEvent(
                type=RoomEventType.AUTH,
                payload={"ok": True, "uid": user.uid, "username": user.username},
            )
        )
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _tick(self) -> None:
        """
        Periodic tick:
        - Determine which session should be active for this room.
        - Drive Arduino UI based on session state.
        - Publish session-changed events.
        """
        now = datetime.now()
        session = self._repo.get_current_session(self._room.room_id, now)

        if self._sessions_equal(self._current_session, session):
            return

        self._current_session = session
        self._bus.publish(
            RoomEvent(
                type=RoomEventType.SESSION_CHANGED,
                payload={
                    "room_id": self._room.room_id,
                    "session": None
                    if session is None
                    else {
                        "session_id": session.session_id,
                        "user_uid": session.user_uid,
                        "start_ts": session.start_ts.isoformat(),
                        "end_ts": session.end_ts.isoformat(),
                        "status": session.status,
                    },
                },
            )
        )

        # Drive Arduino UI, using simple naming conventions if available.
        try:
            if session is None:
                # No active session -> idle
                fn = getattr(self._arduino, "show_idle", None)
                if callable(fn):
                    fn()
            else:
                if session.status == "active":
                    fn = getattr(self._arduino, "show_active", None)
                else:
                    fn = getattr(self._arduino, "show_reserved", None)
                if callable(fn):
                    fn(session.session_id)
        except Exception:
            # Arduino/UI errors should not kill the controller loop
            pass

    @staticmethod
    def _sessions_equal(a: Optional[Session], b: Optional[Session]) -> bool:
        if a is b:
            return True
        if a is None or b is None:
            return False
        return (
            a.session_id == b.session_id
            and a.status == b.status
            and a.start_ts == b.start_ts
            and a.end_ts == b.end_ts
        )

    # ------------------------------------------------------------------
    # Serial integration
    # ------------------------------------------------------------------
    def _on_serial_event(self, ev: Any) -> None:
        """
        Translate low-level ArduinoSerialClient SerialEvent into
        high-level room events.
        """
        try:
            from arduino_serial_client import SerialEventType  # type: ignore
        except Exception:
            return

        if ev.type == SerialEventType.SENSOR:
            self._bus.publish(
                RoomEvent(
                    type=RoomEventType.SENSOR_SNAPSHOT,
                    payload={
                        "sensor": ev.sensor_name,
                        "value": ev.sensor_value,
                        "raw": ev.raw,
                        "ts": ev.ts,
                    },
                )
            )
        elif ev.type in (SerialEventType.LINK_UP, SerialEventType.LINK_DOWN):
            self._bus.publish(
                RoomEvent(
                    type=RoomEventType.LINK_STATE,
                    payload={
                        "state": "up"
                        if ev.type == SerialEventType.LINK_UP
                        else "down",
                        "raw": ev.raw,
                        "ts": ev.ts,
                    },
                )
            )
        elif ev.type == SerialEventType.LOG:
            self._bus.publish(
                RoomEvent(
                    type=RoomEventType.LOG,
                    payload={
                        "module": ev.module,
                        "text": ev.text,
                        "raw": ev.raw,
                        "ts": ev.ts,
                    },
                )
            )

