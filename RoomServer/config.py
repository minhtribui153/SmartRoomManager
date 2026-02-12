from __future__ import annotations
from dataclasses import dataclass

import json
import os

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

def read_json_file(filename):
    """
    Reads a JSON file and handles cases where the file does not exist.

    Args:
        filename (str): The path to the JSON file.

    Returns:
        dict: The data from the JSON file if successful, otherwise empty dict.
    """
    try:
        # Open and read the file
        with open(filename, 'r') as file:
            data = json.load(file)
        return data

    except FileNotFoundError:
        return {}
    
    except json.JSONDecodeError:
        return {}

def write_json_file(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=4)