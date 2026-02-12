from __future__ import annotations
import asyncio

from config import MySqlConfig, RoomConfig
from core.bus import RoomEventBus
from storage.db import Database
from storage.repo import Repo
from arduino.adapter import ArduinoLink
from core.controller import RoomController

async def main() -> None:
    # Load MySQL connection from env.py (room-local DB)
    from env import MYSQL_HOST, MYSQL_USER, MYSQL_PASS, MYSQL_DB

    mysql = MySqlConfig(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
    )
    room = RoomConfig(
        room_id="ROOM-01",
        arduino_port="/dev/ttyACM0",
        enable_org_api=False,  # turn on if needed
        org_api_key="CHANGE_ME",
    )

    db = Database(mysql)
    repo = Repo(db)
    bus = RoomEventBus()

    # Your ArduinoSerialClient must exist at room_manager/arduino/arduino_serial_client.py
    from arduino.arduino_serial_client import ArduinoSerialClient  # type: ignore
    ard_client = ArduinoSerialClient(port=room.arduino_port, baud=room.arduino_baud)
    arduino = ArduinoLink(client=ard_client)

    # Start Arduino client
    arduino.start()

    # Pass serial bus so controller can forward L,0,SENS,<Sensor_name>,... as SENSOR_SNAPSHOT
    controller = RoomController(room, repo, arduino, bus, serial_bus=ard_client.bus)

    # Optional: org API
    if room.enable_org_api:
        import uvicorn
        from org_api.api import build_org_api
        api = build_org_api(controller, bus, api_key=room.org_api_key)
        # run API in background task (separate process is better in production)
        asyncio.create_task(asyncio.to_thread(uvicorn.run, api, host=room.org_api_host, port=room.org_api_port, log_level="info"))

    # Socket server
    from socket_server.server import SocketServer
    sock = SocketServer(room.socket_host, room.socket_port, bus, controller)
    await sock.start()

    try:
        await controller.run_forever()
    finally:
        await sock.stop()
        arduino.stop()
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
