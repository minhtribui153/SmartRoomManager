from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class MySqlConfig:
    host: str
    user: str
    password: str
    database: str
    port: int = 3306

@dataclass(frozen=True)
class RoomConfig:
    room_id: str

    # Arduino serial
    arduino_port: str
    arduino_baud: int = 9600

    # Local socket server (auth + subscribe)
    socket_host: str = "0.0.0.0"
    socket_port: int = 9000

    # Link rules
    link_alive_timeout_s: float = 3.0

    # Org API (API only)
    enable_org_api: bool = False
    org_api_host: str = "0.0.0.0"
    org_api_port: int = 8080
    org_api_key: str = "CHANGE_ME"

    # Tick cadence
    controller_tick_ms: int = 300
