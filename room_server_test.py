import socketio
import uvicorn
import collections.abc
from threading import Thread
from enum import Enum
from datetime import datetime
from time import sleep

from env import MYSQL_HOST, MYSQL_USER, MYSQL_PASS, MYSQL_DB
from config import MySqlConfig
from arduino_serial_client import ArduinoSerialClient, EventBus, SerialEventType
from storage.db import Database

# --- Configuration & Enums ---

class AirQuality(Enum):
    FORCE_IMPULSE = 0
    HIGH_POLLUTION = 1
    LOW_POLLUTION = 2
    FRESH_AIR = 3

# --- Global State ---

# Components
arduino_bus = EventBus()
arduino_cli = ArduinoSerialClient("/dev/ttyACM0", 9600, bus=arduino_bus)
db_config = MySqlConfig(host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASS, database=MYSQL_DB)
db = Database(db_config)

# SocketIO
sio = socketio.AsyncServer(async_mode='asgi')
app = socketio.ASGIApp(sio)

# Logic Controls
current_session_open = False
current_display_sensors = False
current_display_sensors_prev = False
current_onboard_verification = set()

# Sensor Data
current_sensor_temperature = 0.0
current_sensor_air_quality = AirQuality.FRESH_AIR
current_sensor_air_quality_prev = None
current_sensor_motion_detected = False

# Input Timers
current_right_pushbutton_last_pressed = None
current_left_pushbutton_last_pressed = None

# --- Arduino Event Handling ---

def arduino_handle_on_event(ev: SerialEventType):
    global current_sensor_air_quality, current_sensor_motion_detected, \
           current_sensor_temperature, current_display_sensors, \
           current_right_pushbutton_last_pressed

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

    elif ev.type == SerialEventType.LOG:
        print(f"ARDUINO LOG: [{ev.module}] {ev.text}")
    
    elif ev.type == SerialEventType.LINK_UP:
        print("SYSTEM: Arduino Link Established")
    
    elif ev.type == SerialEventType.LINK_DOWN:
        print("SYSTEM: Arduino Link Lost")

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

def update_main_ui(force_init: bool):
    """UI 1: Displays Date/Time and Session Status."""
    # Always update time (Row 0)
    time_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    arduino_cli.send_cmd(f"LCDTXT,0,0,{time_str}")
    
    # Static elements updated once per mode entry
    if force_init:
        arduino_cli.send_cmd("LCDRGB,255,255,255")
        arduino_cli.send_cmd("BUZZER,0")
        if not current_session_open:
            arduino_cli.send_cmd("LCDTXT,0,1,No Session Available")

# --- Background Threads ---

def thread_arduino_ui_manager():
    """Main loop handling display state transitions with pollution priority."""
    global current_display_sensors, current_display_sensors_prev, current_sensor_air_quality
    
    while True:
        # --- PRIORITY LOGIC ---
        # If pollution is detected, force the display to True regardless of buttons
        pollution_detected = current_sensor_air_quality in [
            AirQuality.LOW_POLLUTION, 
            AirQuality.HIGH_POLLUTION, 
            AirQuality.FORCE_IMPULSE
        ]
        
        # Effective state: Either user button is active OR pollution is high
        effective_display_sensors = current_display_sensors or pollution_detected
        
        # Detect state change to trigger cleanup
        mode_switched = (effective_display_sensors != current_display_sensors_prev)
        
        if mode_switched:
            arduino_cli.send_cmd("LCDTXT,2") # Wipe LCD and internal buffers
            sleep(0.1) 

        if effective_display_sensors:
            # Pass pollution_detected to UI to show a "WARNING" message if you like
            update_environment_ui(force_update=mode_switched)
        else:
            update_main_ui(force_init=mode_switched)
        
        current_display_sensors_prev = effective_display_sensors
        sleep(0.5)

def thread_button_timeout_manager():
    """Handles the 5-second timeout to return to the main screen."""
    global current_display_sensors, current_right_pushbutton_last_pressed
    
    while True:
        if current_right_pushbutton_last_pressed:
            delta = (datetime.now() - current_right_pushbutton_last_pressed).total_seconds()
            if delta >= 5.0:
                print("INFO: Sensor display timeout - Returning to Main Screen")
                current_display_sensors = False
                current_right_pushbutton_last_pressed = None
        sleep(0.1)

# --- SocketIO Events ---

@sio.on("connect")
def handle_connect(sid, environ):
    current_onboard_verification.add(sid)
    print(f"SOCKET: Client {sid} connected")

@sio.on('confirm_session')
def handle_confirm_init_session(sid, data):
    if not isinstance(data, collections.abc.Mapping) or "session_code" not in data:
        sio.emit("validate_confirm_session", {"error": True, "msg": "Invalid data object"})

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
    
    # Start Logic Threads
    Thread(target=thread_arduino_ui_manager, daemon=True).start()
    Thread(target=thread_button_timeout_manager, daemon=True).start()
    
    # Start Web Server
    uvicorn.run(app, host="127.0.0.1", port=5000)