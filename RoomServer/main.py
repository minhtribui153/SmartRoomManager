import socketio
import uvicorn
import signal
import random
import collections.abc
import json
import uuid
import os
import asyncio  # Added for emitting from threads

from threading import Thread
from enum import Enum
from datetime import datetime, timedelta
from time import sleep

from env import MYSQL_HOST, MYSQL_USER, MYSQL_PASS, MYSQL_DB
from config import MySqlConfig, read_json_file, write_json_file
from arduino_serial_client import ArduinoSerialClient, EventBus, SerialEventType
from storage.db import Database, ensure_schema
from storage.repo import Repo

import streamlit_shadcn_ui as ui

# --- Configuration & Enums ---
class AirQuality(Enum):
    FORCE_IMPULSE = 0
    HIGH_POLLUTION = 1
    LOW_POLLUTION = 2
    FRESH_AIR = 3

# --- Global State ---
arduino_bus = EventBus()
arduino_cli = ArduinoSerialClient("/dev/ttyACM0", 9600, bus=arduino_bus)
db_config = MySqlConfig(host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASS, database=MYSQL_DB)
db = Database(db_config)
ensure_schema(db)
repo = Repo(db)

# SocketIO
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins="*")
app = socketio.ASGIApp(sio)

# Logic Controls
current_display_sensors = False
current_display_sensors_prev = False
current_onboard_verification = set()
temp_alarm_active = False

# Sensor Data
current_sensor_temperature = 0.0
current_sensor_air_quality = AirQuality.FRESH_AIR
current_sensor_air_quality_prev = None
current_sensor_motion_detected = False
current_right_pushbutton_last_pressed = None

# Session variables
verification_code = None
verification_deadline = None
current_active_session = None
upcoming_session = None

# State tracking of Session variables for UI refreshes
current_display_sensors_prev = False
prev_active_session_id = None
prev_upcoming_session_id = None

# --- Configuration Loading & Setup ---
config: dict = read_json_file("room_config.json")
is_initial_setup = "room_id" not in config

ROOM_ID = config.get("room_id")
ROOM_NAME = config.get("room_name")
ROOM_URL = config.get("url")
ROOM_MAX_TEMP = config.get("max_temp")
ROOM_SECRET = config.get("room_secret")

if not is_initial_setup:
    room = db.fetch_one("SELECT * FROM room_info WHERE room_id = %s", (ROOM_ID,))
    if room:
        ROOM_NAME = room["room_name"]
        ROOM_URL = room["url"]
        ROOM_MAX_TEMP = float(room["max_temp"])
        ROOM_SECRET = room["room_secret"]
        # Sync local config
        config.update({"room_name": ROOM_NAME, "url": ROOM_URL, "max_temp": ROOM_MAX_TEMP, "room_secret": ROOM_SECRET})
        write_json_file("room_config.json", config)
    else:
        is_initial_setup = True

if is_initial_setup:
    print("\n" + "="*30 + "\n==> INITIAL SETUP\n" + "="*30)
    
    room_name_input = input("Assign a permanent name to this room (e.g., Lab A): ").strip()
    ROOM_NAME = room_name_input if room_name_input else "Default Room"

    # FORCED URL INPUT LOOP
    while True:
        url_input = input("Enter the access URL (e.g., http://192.168.1.10:5000): ").strip()
        if url_input.startswith(("http://", "https://")) and "." in url_input:
            ROOM_URL = url_input
            break
        print(">> [ERROR] Invalid URL! Must start with http:// or https:// and contain a domain/IP.")

    temp_input = input("Set Max Temp threshold (Celsius) or leave blank to disable: ").strip()
    ROOM_MAX_TEMP = float(temp_input) if temp_input else None
    
    # AUTHENTICATION SECRET SETUP
    secret_input = input("Set a secret token for client authentication (blank for random): ").strip()
    ROOM_SECRET = secret_input if secret_input else str(uuid.uuid4())[:16].upper()
    
    ROOM_ID = str(uuid.uuid4())[:8].upper()
    
    db.execute(
        "INSERT INTO room_info (room_id, room_name, url, max_temp, room_secret) VALUES (%s, %s, %s, %s, %s);",
        (ROOM_ID, ROOM_NAME, ROOM_URL, ROOM_MAX_TEMP, ROOM_SECRET)
    )
    
    config.update({"room_id": ROOM_ID, "room_name": ROOM_NAME, "url": ROOM_URL, "max_temp": ROOM_MAX_TEMP, "room_secret": ROOM_SECRET})
    write_json_file("room_config.json", config)
    print(f"==> Setup Complete! ID: {ROOM_ID} | Secret: {ROOM_SECRET}")
    
running = True

# --- Arduino Event Handling ---
def arduino_handle_on_event(ev: SerialEventType):
    global current_sensor_air_quality, current_sensor_motion_detected, \
            current_sensor_temperature, current_display_sensors, \
            current_right_pushbutton_last_pressed, \
            verification_code, verification_deadline, current_active_session

    if ev.type == SerialEventType.SENSOR:
        if ev.sensor_name == "AIR_QUALITY":
            # Extract quality integer from "value,quality" string
            try:
                quality_val = int(ev.sensor_value.split(",")[1])
                current_sensor_air_quality = AirQuality(quality_val)
            except (IndexError, ValueError):
                pass
        
        elif ev.sensor_name == "TEMP":
            current_sensor_temperature = float(ev.sensor_value)
        
        elif ev.sensor_name == "MOTION":
            current_sensor_motion_detected = int(ev.sensor_value) == 1

    elif ev.type == SerialEventType.UI:
        if ev.component_name == "BUTTON" and ev.component_value == "R":
            print("INFO: Right button pressed - Showing Sensors")
            current_right_pushbutton_last_pressed = datetime.now()
            current_display_sensors = True
        elif ev.component_name == "BUTTON" and ev.component_value == "L":
            if verification_deadline and current_active_session is None:
                verification_code = random.randint(1000, 9999)
                print(f"VERIFICATION: Code {verification_code} generated for local user.")
    
    elif ev.type == SerialEventType.LINK_UP:
        print("SYSTEM:   Arduino Link Established")
    
    elif ev.type == SerialEventType.LINK_DOWN:
        print("SYSTEM:   Arduino Link Lost")

# --- Display Logic ---
def update_environment_ui(force_update: bool):
    """UI 2: Displays Temperature and Air Quality."""
    global current_sensor_air_quality_prev, current_sensor_temperature, current_sensor_air_quality
    
    aqi_map = {
        AirQuality.FRESH_AIR:      ["Fresh Air", "0", "0,255,0"],
        AirQuality.LOW_POLLUTION:  ["Low Pollution! Area might be unfit for use.", "3", "255,255,0"],
        AirQuality.HIGH_POLLUTION: ["High Pollution! Please evacuate the area.", "4", "255,0,0"],
        AirQuality.FORCE_IMPULSE:  ["Unstable Pollution! Please evacuate the area.", "4", "255,0,0"]
    }

    # Always update temperature (Row 0)
    arduino_cli.send_cmd(f"LCDTXT,1,0,Temp,{current_sensor_temperature} C")
    
    # Update AQI (Row 1) and RGB only on change or mode entry
    if force_update or current_sensor_air_quality != current_sensor_air_quality_prev:
        text, buzz, rgb = aqi_map[current_sensor_air_quality]
        arduino_cli.send_cmd(f"LCDTXT,1,1,AQI,{text}")
        arduino_cli.send_cmd(f"LCDRGB,{rgb}")
        arduino_cli.send_cmd(f"BUZZER,{buzz}")
        current_sensor_air_quality_prev = current_sensor_air_quality

        # Emit to Dashboard if pollution is detected
        if current_sensor_air_quality != AirQuality.FRESH_AIR:
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(sio.emit("room_alert", {
                    "type": "air_quality", 
                    "val": current_sensor_air_quality.name,
                    "msg": text
                }))
            except: pass

def update_main_ui(force_init: bool):
    """UI: Displays Session info on the LCD."""
    now_str = datetime.now().strftime("%H:%M")
    
    if current_active_session:
        # If a session is open, show the user or session ID
        line2 = f"User: {current_active_session.user_uid[:10]}"
    elif upcoming_session:
        # If no session now, show when the next one starts
        start_time = upcoming_session.start_ts.strftime("%H:%M")
        line2 = f"Next: {start_time}"
    else:
        line2 = "No Bookings Available"

    arduino_cli.send_cmd(f"LCDRGB,255,255,255")
    if force_init:
        arduino_cli.send_cmd(f"LCDTXT,1,0,{now_str},{ROOM_NAME}")
        arduino_cli.send_cmd(f"LCDTXT,0,1,{line2}")
        arduino_cli.send_cmd(f"BUZZER,0")

def update_verification_ui(force_init: bool):
    global verification_deadline, verification_code
    
    # Calculate remaining time
    remaining = (verification_deadline - datetime.now()).total_seconds()
    mins, secs = divmod(max(0, int(remaining)), 60)
    
    # Line 1: Header
    line1 = "Verify Presence"
    
    # Line 2: Either show the Code (if button was pressed) or the Timer
    if verification_code:
        line2 = f"Code: {verification_code}"
    else:
        line2 = f"Ends in {mins:02d}:{secs:02d}"

    arduino_cli.send_cmd(f"LCDTXT,0,0,{line1}")
    arduino_cli.send_cmd(f"LCDTXT,0,1,{line2}")

def update_temp_alert_ui(force_update: bool):
    """New UI function for Temperature Alerts."""
    # LCD Text
    arduino_cli.send_cmd(f"LCDTXT,0,0,Temp Warning!")
    arduino_cli.send_cmd(f"LCDTXT,0,1,Temp: {current_sensor_temperature} C")
    
    # Yellow RGB and Special Buzzer
    arduino_cli.send_cmd("LCDRGB,255,255,0") # Yellow
    arduino_cli.send_cmd("BUZZER,3")         # Pulsing alarm

# --- Background Threads ---
first_run = True
def thread_arduino_ui_manager():
    global current_display_sensors, current_display_sensors_prev, first_run, running, temp_alarm_active
    global prev_active_session_id, prev_upcoming_session_id # Access global trackers

    while running:
        # --- 1. SENSOR LOGIC ---
        pollution_detected = current_sensor_air_quality in [AirQuality.LOW_POLLUTION, AirQuality.HIGH_POLLUTION, AirQuality.FORCE_IMPULSE]
        temp_alarm_active = (ROOM_MAX_TEMP is not None and current_sensor_temperature > ROOM_MAX_TEMP)
        effective_mode = pollution_detected or temp_alarm_active or current_display_sensors

        # --- 2. SESSION CHANGE DETECTION ---
        # Get current IDs (handle None if no session exists)
        curr_act_id = current_active_session.session_id if current_active_session else None
        curr_upc_id = upcoming_session.session_id if upcoming_session else None

        # Check if IDs have changed since last loop
        session_changed = (curr_act_id != prev_active_session_id) or (curr_upc_id != prev_upcoming_session_id)

        # --- 3. MODE SWITCH LOGIC ---
        # Switch if: Mode changed (Sensors vs Main) OR it's first run OR a session changed
        mode_switched = (effective_mode != current_display_sensors_prev) or first_run or session_changed
        
        if mode_switched:
            arduino_cli.send_cmd("LCDTXT,2") # Clear LCD
            sleep(0.1)

        # --- 4. RENDER LOGIC ---
        if pollution_detected:
            update_environment_ui(force_update=mode_switched)
        elif temp_alarm_active:
            update_temp_alert_ui(force_update=mode_switched)
        elif current_display_sensors:
            update_environment_ui(force_update=mode_switched)
        else:
            now = datetime.now()
            is_pending_verification = (
                verification_deadline is not None and 
                current_active_session is None and 
                now < verification_deadline
            )

            if is_pending_verification:
                update_verification_ui(force_init=mode_switched)
            else:
                # This will now trigger with force_init=True if a session changed
                update_main_ui(force_init=mode_switched)
        
        # --- 5. CLEANUP ---
        # Update trackers for the next loop iteration
        current_display_sensors_prev = effective_mode
        prev_active_session_id = curr_act_id
        prev_upcoming_session_id = curr_upc_id
        first_run = False
        
        sleep(0.5)

def thread_button_timeout_manager():
    """Handles the 5-second timeout to return to the main screen."""
    global current_display_sensors, current_right_pushbutton_last_pressed, running
    
    while running:
        if current_right_pushbutton_last_pressed:
            delta = (datetime.now() - current_right_pushbutton_last_pressed).total_seconds()
            if delta >= 5.0:
                print("INFO: Sensor display timeout - Returning to Main Screen")
                current_display_sensors = False
                current_right_pushbutton_last_pressed = None
        sleep(0.1)

def thread_room_log_sensors():
    global running
    while running:
        db.execute(
            """
            INSERT INTO room_sensor_logs (room_id, temp, air_quality, motion_detected)
            VALUES (%s, %s, %s, %s)
            """,
            (
                ROOM_ID,
                current_sensor_temperature,
                current_sensor_air_quality.name.lower(),
                1 if current_sensor_motion_detected else 0
            )
        )
        sleep(10)

def thread_room_server_checker():
    global running, ROOM_ID
    while running:
        try:
            # FIX: Parameters must be a tuple (ROOM_ID,) not (ROOM_ID)
            room = db.fetch_one("SELECT * FROM room_info WHERE room_id = %s", (ROOM_ID,))
            
            # If the query runs but returns NOTHING, then the room was deleted.
            if room is None:
                print("CRITICAL: Room record deleted from Database. Shutting down.")
                running = False
                arduino_cli.stop()
                os.kill(os.getpid(), signal.SIGTERM)
                
        except Exception as e:
            # If there is a DB error (like a timeout), don't kill the server!
            # Just log it and try again in 10 seconds.
            print(f"WARNING: Database check failed (busy or disconnected): {e}")
            
        sleep(10)

def thread_session_synchronizer():
    global running, ROOM_ID, verification_deadline, current_active_session, upcoming_session
    
    while running:
        try:
            now = datetime.now()
            
            # 1. Sync the currently ACTIVE session from DB
            # This ensures that if the server restarts, it knows a session is active
            current_active_session = repo.get_current_room_session(ROOM_ID, now)

            # 2. Fetch upcoming sessions
            upcoming = repo.get_upcoming_sessions(ROOM_ID, now)
            next_s = upcoming[0] if upcoming else None
            
            # CRITICAL FIX: Assign to global variable so UI can display it
            upcoming_session = next_s

            if next_s and next_s.status == 'scheduled':
                # 3. Handle the 10-minute Verification Window
                if now >= next_s.start_ts:
                    if not verification_deadline:
                        verification_deadline = next_s.start_ts + timedelta(minutes=10)
                    
                    # Check for timeout (No-show)
                    if now > verification_deadline:
                        print(f"TIMEOUT: Session {next_s.session_id} expired. Deleting.")
                        db.execute("DELETE FROM sessions WHERE session_id = %s", (next_s.session_id,))
                        verification_deadline = None
                        upcoming_session = None # Clear after deletion
                else:
                    # Session is in the future, wait for start time
                    verification_deadline = None
            else:
                # No scheduled session or it's already active/cancelled
                verification_deadline = None

        except Exception as e:
            print(f"Sync Error: {e}")
        sleep(10)

# --- SocketIO Events ---

@sio.on("connect")
async def handle_connect(sid, environ, auth=None):
    """
    BLANK FOR AUTHENTICATION: 
    This checks the 'token' passed during the connection handshake.
    """
    if auth and auth.get("token") == ROOM_SECRET:
        current_onboard_verification.add(sid)
        print(f"SOCKET: Authenticated client {sid} connected.")
    else:
        print(f"SOCKET: Unauthorized connection attempt from {sid}.")
        return False # Refuse connection

@sio.on("start_presence_verification")
async def handle_presence(sid, data):
    global is_verifying
    is_verifying = True
    print(f"SOCKET: User at {sid} is present, starting verification.")

@sio.on("verify_code")
async def handle_verify_code(sid, data):
    global verification_code, current_active_session, upcoming_session, is_verifying
    input_code = data.get("code")
    
    # Check if we actually have an upcoming session to verify
    if upcoming_session and str(input_code) == str(verification_code):
        # Success! 
        db.execute("UPDATE sessions SET status = 'active' WHERE session_id = %s", 
                   (upcoming_session.session_id,))
        
        # Update local state immediately
        current_active_session = upcoming_session
        verification_code = None
        is_verifying = False
        
        await sio.emit("verification_result", {"success": True}, to=sid)
        print(f"VERIFICATION: Session {current_active_session.session_id} is now ACTIVE.")
    else:
        await sio.emit("verification_result", {"success": False, "msg": "Invalid Code"}, to=sid)

@sio.event
def disconnect(sid):
    if sid in current_onboard_verification:
        current_onboard_verification.remove(sid)
    print(f"SOCKET: Client {sid} disconnected")

# --- Main Entry Point ---

if __name__ == '__main__':
    # Initialize Arduino
    arduino_bus.subscribe(arduino_handle_on_event)
    arduino_cli.start()
    
    # Start all Threads
    Thread(target=thread_arduino_ui_manager, daemon=True).start()
    Thread(target=thread_button_timeout_manager, daemon=True).start()
    Thread(target=thread_room_server_checker, daemon=True).start()
    Thread(target=thread_session_synchronizer, daemon=True).start()
    Thread(target=thread_room_log_sensors, daemon=True).start()
    
    # Start Web Server
    uvicorn.run(app, host="0.0.0.0", port=5000)