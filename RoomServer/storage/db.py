from __future__ import annotations
import threading

"""
MySQL database wrapper using PyMySQL.

This is a thin helper around a single process-local connection. It is
intentionally simple and room-local as per SPEC: the DB runs on the Pi,
and we use it for persistence + auditing, not for complex ORM behaviour.
"""

from contextlib import contextmanager
from typing import Any, Dict, Iterable, Optional

import pymysql
from pymysql.cursors import DictCursor

from config import MySqlConfig


class Database:
    """Room-local MySQL connection helper."""

    def __init__(self, cfg: MySqlConfig) -> None:
        self._cfg = cfg
        self._conn = self._connect()
        self._lock = threading.Lock()

    def _connect(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self._cfg.host,
            user=self._cfg.user,
            password=self._cfg.password,
            database=self._cfg.database,
            port=self._cfg.port,
            cursorclass=DictCursor,
            autocommit=True,
            charset="utf8mb4",
        )

    @property
    def conn(self) -> pymysql.connections.Connection:
        # Simple "reconnect if closed" logic
        try:
            self._conn.ping(reconnect=True)
        except Exception:
            self._conn = self._connect()
        return self._conn

    @contextmanager
    def cursor(self):
        cur = self.conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # -----------------------------
    # Convenience helpers
    # -----------------------------
    # Update these three methods to use the lock:
    def execute(self, sql: str, params: Optional[Iterable[Any]] = None) -> int:
        with self._lock: # Lock during execution
            with self.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.rowcount

    def fetch_one(self, sql: str, params: Optional[Iterable[Any]] = None) -> Optional[Dict[str, Any]]:
        with self._lock: # Lock during execution
            with self.cursor() as cur:
                cur.execute(sql, params or ())
                row = cur.fetchone()
            return row

    def fetch_all(self, sql: str, params: Optional[Iterable[Any]] = None) -> list[Dict[str, Any]]:
        with self._lock: # Lock during execution
            with self.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall()
            return list(rows)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def ensure_schema(db: Database) -> None:
    """
    Create minimal tables needed for the room controller + dashboards.

    This is intentionally small; it can be evolved as needed.
    """

    # Simple users table: org-managed user directory.
    # - uid: organisation's user ID
    # - username: human-readable login / username
    # - user_hash: current verifiable hash (can be rotated by org app)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            uid VARCHAR(64) NOT NULL UNIQUE,
            username VARCHAR(255) NOT NULL,
            user_hash VARCHAR(128) NOT NULL,
            hash_expires_at DATETIME NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

    # 1. Room Sessions: The scheduled booking
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            session_id VARCHAR(64) NOT NULL UNIQUE,
            room_id VARCHAR(64) NOT NULL,
            user_uid VARCHAR(64) NOT NULL,
            start_ts DATETIME NOT NULL,
            end_ts DATETIME NOT NULL,
            status ENUM('scheduled', 'active', 'completed', 'cancelled') NOT NULL DEFAULT 'scheduled'
        ) ENGINE=InnoDB;
    """)

    # 2. Login Sessions: The authentication state
    db.execute("""
        CREATE TABLE IF NOT EXISTS login_sessions (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            room_session_id VARCHAR(64) NOT NULL,
            user_uid VARCHAR(64) NOT NULL,
            login_token VARCHAR(128) NOT NULL, 
            expires_at DATETIME NOT NULL,
            authenticated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_room_session (room_session_id),
            FOREIGN KEY (room_session_id) REFERENCES sessions(session_id)
        ) ENGINE=InnoDB;
    """)

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS room_sensor_logs (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            room_id VARCHAR(16) NOT NULL,
            temp DECIMAL(5,2) NOT NULL,
            air_quality ENUM('force_signal', 'high_pollution', 'low_pollution', 'fresh_air') NOT NULL DEFAULT 'fresh_air',
            motion_detected TINYINT(1) NOT NULL DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS room_info (
            room_id VARCHAR(16) PRIMARY KEY NOT NULL,
            room_name VARCHAR(32),
            url VARCHAR(255),
            max_temp DECIMAL(5,2) NULL  -- Added max_temp column
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
    )

