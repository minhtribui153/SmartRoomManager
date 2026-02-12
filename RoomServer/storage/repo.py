from __future__ import annotations

"""
Repository layer over the room-local MySQL database.

This keeps SQL in one place and presents higher-level operations to the
room controller and dashboards (sessions, auth, simple metrics).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List

from storage.db import Database


@dataclass(frozen=True)
class Session:
    session_id: str
    room_id: str
    user_uid: str
    start_ts: datetime
    end_ts: datetime
    status: str


@dataclass(frozen=True)
class User:
    uid: str
    username: str
    user_hash: str
    hash_expires_at: Optional[datetime]


class Repo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -------------------------
    # Users
    # -------------------------
    def upsert_user(
        self,
        uid: str,
        username: str,
        user_hash: str,
        hash_expires_at: Optional[datetime] = None,
    ) -> None:
        """
        Helper for local tools/tests. In production, the organisation's web
        application is normally responsible for managing users.
        """
        self._db.execute(
            """
            INSERT INTO users (uid, username, user_hash, hash_expires_at)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                username = VALUES(username),
                user_hash = VALUES(user_hash),
                hash_expires_at = VALUES(hash_expires_at);
            """,
            (uid, username, user_hash, hash_expires_at),
        )

    def get_user(self, uid: str) -> Optional[User]:
        row = self._db.fetch_one(
            """
            SELECT uid, username, user_hash, hash_expires_at
            FROM users
            WHERE uid = %s;
            """,
            (uid,),
        )
        if not row:
            return None
        return User(**row)

    def get_user_by_hash(
        self,
        user_hash: str,
        now: Optional[datetime] = None,
    ) -> Optional[User]:
        """
        Look up a user by their hash key (used by the exposed Streamlit
        dashboard when the org web app embeds ?user_hash=... in the URL).
        """
        row = self._db.fetch_one(
            """
            SELECT uid, username, user_hash, hash_expires_at
            FROM users
            WHERE user_hash = %s;
            """,
            (user_hash,),
        )
        if not row:
            return None

        if row["hash_expires_at"] is not None:
            if now is None:
                now = datetime.now()
            if row["hash_expires_at"] < now:
                return None

        return User(**row)

    # -------------------------
    # Sessions
    # -------------------------
    def get_current_room_session(self, room_id: str, now: datetime) -> Optional[Session]:
        """Checks if the room is currently booked."""
        row = self._db.fetch_one(
            """
            SELECT session_id, room_id, user_uid, start_ts, end_ts, status
            FROM sessions
            WHERE room_id = %s AND start_ts <= %s AND end_ts >= %s
            AND status IN ('scheduled', 'active')
            LIMIT 1;
            """, (room_id, now, now)
        )
        return Session(**row) if row else None

    def get_upcoming_sessions(
        self, room_id: str, from_ts: datetime, limit: int = 10
    ) -> List[Session]:
        rows = self._db.fetch_all(
            """
            SELECT session_id, room_id, user_uid, start_ts, end_ts, status
            FROM sessions
            WHERE room_id = %s
              AND start_ts >= %s
            ORDER BY start_ts ASC
            LIMIT %s;
            """,
            (room_id, from_ts, limit),
        )
        return [Session(**r) for r in rows]

    def get_sessions_for_user(
        self,
        room_id: str,
        user_uid: str,
        from_ts: datetime,
        limit: int = 10,
    ) -> List[Session]:
        """
        All sessions for a given user in this room from a given time onward.
        """
        rows = self._db.fetch_all(
            """
            SELECT session_id, room_id, user_uid, start_ts, end_ts, status
            FROM sessions
            WHERE room_id = %s
              AND user_uid = %s
              AND end_ts >= %s
            ORDER BY start_ts ASC
            LIMIT %s;
            """,
            (room_id, user_uid, from_ts, limit),
        )
        return [Session(**r) for r in rows]

    def get_current_session_for_user(
        self,
        room_id: str,
        user_uid: str,
        now: datetime,
    ) -> Optional[Session]:
        row = self._db.fetch_one(
            """
            SELECT session_id, room_id, user_uid, start_ts, end_ts, status
            FROM sessions
            WHERE room_id = %s
              AND user_uid = %s
              AND start_ts <= %s
              AND end_ts >= %s
            ORDER BY start_ts ASC
            LIMIT 1;
            """,
            (room_id, user_uid, now, now),
        )
        if not row:
            return None
        return Session(**row)

    def create_session(
        self,
        session_id: str,
        room_id: str,
        user_uid: str,
        start_ts: datetime,
        end_ts: datetime,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO sessions (session_id, room_id, user_uid, start_ts, end_ts, status)
            VALUES (%s, %s, %s, %s, %s, 'scheduled')
            ON DUPLICATE KEY UPDATE
                user_uid = VALUES(user_uid),
                start_ts = VALUES(start_ts),
                end_ts = VALUES(end_ts);
            """,
            (session_id, room_id, user_uid, start_ts, end_ts),
        )

    def update_session_status(self, session_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE sessions SET status = %s WHERE session_id = %s;",
            (status, session_id),
        )

    # -------------------------
    # Auth (org-managed user hash + permanent users table)
    # -------------------------
    def set_session_hash(
        self,
        session_id: str,
        user_hash: str,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """
        Helper for local tools/tests. In production, the organisation's web
        application is expected to manage this table directly.
        """
        self._db.execute(
            """
            INSERT INTO session_auth (session_id, user_hash, expires_at)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                user_hash = VALUES(user_hash),
                expires_at = VALUES(expires_at);
            """,
            (session_id, user_hash, expires_at),
        )

    def validate_user_hash(
        self,
        session_id: str,
        user_uid: str,
        provided_user_hash: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Validate authentication using the org-managed user hash.

        - `session_id` and `user_uid` must match the scheduled session
        - `provided_user_hash` must equal the stored user_hash
        - If expires_at is set, it must be >= now
        """
        row = self._db.fetch_one(
            """
            SELECT sa.user_hash, sa.expires_at, s.user_uid
            FROM session_auth sa
            JOIN sessions s ON s.session_id = sa.session_id
            WHERE sa.session_id = %s;
            """,
            (session_id,),
        )
        if not row:
            return False

        if row["user_uid"] != user_uid:
            return False

        if row["user_hash"] != provided_user_hash:
            return False

        if row["expires_at"] is not None:
            if now is None:
                now = datetime.now()
            if row["expires_at"] < now:
                return False

        return True

    def create_login_session(self, room_session_id: str, user_uid: str, token: str, expiry: datetime):
        """Creates a login session linked to a specific room booking."""
        self._db.execute(
            """
            INSERT INTO login_sessions (room_session_id, user_uid, login_token, expires_at)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE login_token = VALUES(login_token), expires_at = VALUES(expires_at);
            """, (room_session_id, user_uid, token, expiry)
        )

    def is_user_logged_in(self, room_session_id: str, token: str, now: datetime) -> bool:
        """Validates that the user has an active login for this specific booking."""
        row = self._db.fetch_one(
            """
            SELECT id FROM login_sessions
            WHERE room_session_id = %s AND login_token = %s AND expires_at > %s;
            """, (room_session_id, token, now)
        )
        return row is not None