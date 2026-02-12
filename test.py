import time
from arduino_serial_client import ArduinoSerialClient, EventBus, SerialEventType
from time import sleep

PORT = "/dev/ttyACM0"   # change if needed
BAUD = 9600


def on_event(ev):
    if ev.type == SerialEventType.LOG:
        print(f"[LOG][{ev.module}] {ev.text}")
    elif ev.type == SerialEventType.RSP:
        print(f"[RSP] {ev.raw}")
    elif ev.type == SerialEventType.SENSOR:
        print(f"[SENS] [{ev.sensor_name}] {ev.sensor_value}")
    elif ev.type == SerialEventType.LINK_UP:
        print(">>> LINK UP")
    elif ev.type == SerialEventType.LINK_DOWN:
        print(">>> LINK DOWN")
    elif ev.type == SerialEventType.ERROR:
        print(f"!!! ERROR: {ev.raw}")
    else:
        print(f"[RAW] {ev.raw}")


def main():
    bus = EventBus()
    bus.subscribe(on_event)

    cli = ArduinoSerialClient(
        port=PORT,
        baud=BAUD,
        bus=bus,
    )

    print("Starting client...")
    cli.start()
    sleep(5)

    try:

        # Send a command
        print("Sending LCDTXT command")
        cli.send_cmd("LCDTXT,0,0,Hello From Pi!")
        cli.send_cmd("LCDRGB,0,0,255")

        time.sleep(5)

        print("Sending BUZZER command")
        cli.send_cmd("BUZZER,4")

        time.sleep(10)

        cli.send_cmd("BUZZER,0")

        # Check status snapshot
        st = cli.get_status()
        print("STATUS:", st)

    finally:
        print("Stopping client...")
        cli.stop()


if __name__ == "__main__":
    main()
