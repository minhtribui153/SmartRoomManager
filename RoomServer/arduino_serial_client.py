from __future__ import annotations

import time
import threading
import queue
from enum import Enum
from dataclasses import dataclass
from typing import Callable, Optional, List

import serial

class CommandError(RuntimeError):
    pass

class SerialEventType(str, Enum):
    """Serial event kinds. Arduino is the output interface: we send CMDs; it responds and logs."""
    LOG = "log"          # L,<type>,<module>,<text>
    UI = "ui"
    # Sensor logs (observed from Arduino): L,0,SENS,<Sensor_Name>,...
    #   TEMP:        L,0,SENS,TEMP,<temperature_degC>
    #   AIR_QUALITY: L,0,SENS,AIR_QUALITY,<value>,<slope>   (Grove Air Quality v1.3 getValue(), slope())
    #   MOTION:      L,0,SENS,MOTION,<0|1>   (1=detected, 0=no motion)
    SENSOR = "sensor"
    RSP = "rsp"          # R,<code>,<msg>  command response
    RAW = "raw"          # anything else (e.g. P,0,ALIVE)
    LINK_UP = "link_up"
    LINK_DOWN = "link_down"
    ERROR = "error"


@dataclass(frozen=True)
class SerialEvent:
    type: SerialEventType
    ts: float  # time.monotonic()
    raw: str   # original line (or message)
    module: Optional[str] = None
    log_type: Optional[int] = None
    text: Optional[str] = None
    sensor_name: Optional[str] = None   # for SENSOR: name from L,0,SENS,<Sensor_name>,...
    sensor_value: Optional[str] = None  # rest after sensor name
    component_name: Optional[str] = None
    component_value: Optional[str] = None


# Subscriber signature
SerialSubscriber = Callable[[SerialEvent], None]

class EventBus:
    """
    In-process pub/sub.
    - subscribe() returns an unsubscribe function
    - publish() calls subscribers (safe snapshot)
    """

    def __init__(self):
        self._subs: List[SerialSubscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: SerialSubscriber):
        with self._lock:
            self._subs.append(fn)

        def unsubscribe():
            with self._lock:
                try:
                    self._subs.remove(fn)
                except ValueError:
                    pass

        return unsubscribe

    def publish(self, ev: SerialEvent):
        with self._lock:
            subs = list(self._subs)

        # Call outside lock to avoid deadlocks
        for fn in subs:
            try:
                fn(ev)
            except Exception:
                # swallow subscriber errors so one handler can't kill serial loop
                pass

@dataclass
class ArduinoSerialStatus:
    port: str
    baud: int
    is_open: bool
    alive: bool
    last_err: str
    seconds_since_last_rx: Optional[float]
    seconds_since_last_tx: Optional[float]
    rx_lines: int
    tx_lines: int
    tx_queue_size: int
    supervisor_running: bool


class ArduinoSerialClient:
    """
    Arduino serial client: output interface to the room hardware.

    - We send commands via send_cmd(); Arduino responds with R,<code>,<msg>.
    - Arduino sends log lines: L,<type>,<module>,<text>.
      Sensor logs use: L,0,SENS,<Sensor_name>,<value_or_rest> (e.g. L,0,SENS,Temperature,25.5).
    - Heartbeat: Arduino replies P,0,ALIVE to PING; alive is True only when received recently.
    - PING is sent every 1 second (ping_interval_s) to maintain the connection.
    - Boot: Opening serial resets the Arduino. It sends L,0,SYS,BOOT then ~30s later L,0,SYS,SENSORS_READY.

    Key behaviors:
    - start(): launches supervisor thread until stop()
    - alive: True only after recent "P,0,ALIVE"
    - PING every 1s from writer thread; auto reconnect on failure
    """

    def __init__(
        self,
        port: str,
        baud: int = 9600,
        read_timeout_s: float = 0.2,
        ping_interval_s: float = 1.0,  # send PING every 1s to maintain connection
        alive_timeout_s: float = 3.0,
        reconnect_backoff_s: float = 1.0,
        bus: Optional[EventBus] = None,
    ):
        self._port = port
        self._baud = baud
        self._read_timeout_s = read_timeout_s
        self._ping_interval_s = ping_interval_s
        self._alive_timeout_s = alive_timeout_s
        self._reconnect_backoff_s = reconnect_backoff_s

        self.bus = bus or EventBus()

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._fatal_evt = threading.Event()

        self._tx_q: "queue.Queue[str]" = queue.Queue()

        self._ser: Optional[serial.Serial] = None
        self._t_sup: Optional[threading.Thread] = None
        self._t_rd: Optional[threading.Thread] = None
        self._t_wr: Optional[threading.Thread] = None

        self._cmd_cv = threading.Condition(self._lock)
        self._cmd_waiting = False
        self._cmd_deadline = 0.0
        self._cmd_reply: Optional[str] = None

        # state
        self._is_open = False
        self._last_err = ""
        self._last_rx_at = 0.0
        self._last_tx_at = 0.0
        self._last_alive_ack_at = 0.0
        self._rx_lines = 0
        self._tx_lines = 0

        # link events throttle/state
        self._alive = False
        self._last_alive_check = 0.0

    # --------------------
    # Public API
    # --------------------
    def start(self) -> None:
        """
        Start supervisor thread (idempotent).
        Keeps running until stop().
        """
        with self._lock:
            if self._t_sup and self._t_sup.is_alive():
                return

        self._stop_evt.clear()
        self._fatal_evt.clear()

        self._t_sup = threading.Thread(
            target=self._supervisor_loop,
            name="arduino-serial-supervisor",
            daemon=True,
        )
        self._t_sup.start()
        time.sleep(5)
        

    def stop(self) -> None:
        """
        Stop everything and close serial. Safe to call multiple times.
        """
        self._stop_evt.set()
        self._fatal_evt.set()

        # join supervisor
        if self._t_sup and self._t_sup.is_alive():
            self._t_sup.join(timeout=1.0)

        # ensure workers stopped + port closed
        self._close_serial()

        # drain tx queue
        try:
            while True:
                self._tx_q.get_nowait()
        except queue.Empty:
            pass

        with self._lock:
            self._alive = False

    def send_cmd(self, line: str, timeout_s: float = 10.0) -> str:
        """
        Send a command and wait for an R,<code>,<msg> response.

        Returns:
            msg (e.g. "OK" for "R,0,OK")

        Raises:
            CommandError("ARGS") for "R,1,ARGS"
            CommandError("TIMEOUT") if no response in timeout
        """
        try:
            if not line:
                raise CommandError("EMPTY")

            # Only allow one in-flight command (no correlation IDs in protocol)
            with self._cmd_cv:
                if not self._is_open:
                    raise CommandError("DISCONNECTED")

                # Wait until no other command is waiting
                start_wait = time.monotonic()
                while self._cmd_waiting and not self._stop_evt.is_set():
                    # Don't block forever if caller spams send_cmd
                    if time.monotonic() - start_wait > timeout_s:
                        raise CommandError("BUSY")
                    self._cmd_cv.wait(timeout=0.05)

                self._cmd_waiting = True
                self._cmd_reply = None
                self._cmd_deadline = time.monotonic() + timeout_s

                # enqueue command
                self._tx_q.put("CMD," + line)

                # wait for reply
                while self._cmd_reply is None and not self._stop_evt.is_set():
                    remaining = self._cmd_deadline - time.monotonic()
                    if remaining <= 0:
                        self._cmd_waiting = False
                        self._cmd_cv.notify_all()
                        raise CommandError("TIMEOUT")
                    self._cmd_cv.wait(timeout=min(0.2, remaining))

                if self._cmd_reply is None:
                    self._cmd_waiting = False
                    self._cmd_cv.notify_all()
                    raise CommandError("TIMEOUT")

                reply = self._cmd_reply
                self._cmd_reply = None
                self._cmd_waiting = False
                self._cmd_cv.notify_all()

            # Parse reply outside lock
            # Expected: R,<code>,<msg...>
            parts = reply.split(",", 2)
            if len(parts) < 2 or parts[0] != "R":
                raise CommandError("BAD_REPLY")

            code = parts[1].strip()
            msg = parts[2].strip() if len(parts) >= 3 else ""

            if code == "0":
                return msg or "OK"
            else:
                # error: "R,1,ARGS" -> CommandError("ARGS")
                raise CommandError(msg or "ERROR")
        except Exception as e:
            print(e)
            return None


    def get_status(self) -> ArduinoSerialStatus:
        now = time.monotonic()
        with self._lock:
            is_open = self._is_open
            last_err = self._last_err
            last_rx = self._last_rx_at
            last_tx = self._last_tx_at
            rx_lines = self._rx_lines
            tx_lines = self._tx_lines
            alive = self._compute_alive_locked(now)
            sup_running = bool(self._t_sup and self._t_sup.is_alive())

        return ArduinoSerialStatus(
            port=self._port,
            baud=self._baud,
            is_open=is_open,
            alive=alive,
            last_err=last_err,
            seconds_since_last_rx=None if last_rx == 0.0 else (now - last_rx),
            seconds_since_last_tx=None if last_tx == 0.0 else (now - last_tx),
            rx_lines=rx_lines,
            tx_lines=tx_lines,
            tx_queue_size=self._tx_q.qsize(),
            supervisor_running=sup_running,
        )

    # --------------------
    # Supervisor
    # --------------------
    def _supervisor_loop(self) -> None:
        while not self._stop_evt.is_set():
            if not self._open_serial():
                time.sleep(self._reconnect_backoff_s)
                continue

            self._fatal_evt.clear()

            self._t_rd = threading.Thread(target=self._reader_loop, name="arduino-serial-reader", daemon=True)
            self._t_wr = threading.Thread(target=self._writer_loop, name="arduino-serial-writer", daemon=True)
            self._t_rd.start()
            self._t_wr.start()

            # wait until stop or fatal or worker died
            while not self._stop_evt.is_set() and not self._fatal_evt.is_set():
                if (self._t_rd and not self._t_rd.is_alive()) or (self._t_wr and not self._t_wr.is_alive()):
                    self._fatal_evt.set()
                    break
                self._maybe_emit_link_state(time.monotonic())
                time.sleep(0.05)

            self._close_serial()

            if self._stop_evt.is_set():
                break

            time.sleep(self._reconnect_backoff_s)

    # --------------------
    # Serial open/close
    # --------------------
    def _open_serial(self) -> bool:
        try:
            ser = serial.Serial()
            ser.port = self._port
            ser.baudrate = self._baud
            ser.timeout = self._read_timeout_s
            ser.dtr = False
            ser.rts = False
            ser.open()
        except Exception as e:
            with self._lock:
                self._ser = None
                self._is_open = False
                self._alive = False
                self._last_err = f"open failed: {e}"
            self.bus.publish(SerialEvent(SerialEventType.ERROR, time.monotonic(), self._last_err))
            return False

        now = time.monotonic()
        with self._lock:
            self._ser = ser
            self._is_open = True
            self._last_err = ""
            self._last_rx_at = 0.0
            self._last_tx_at = now
            self._last_alive_ack_at = 0.0
            self._rx_lines = 0
            self._tx_lines = 0
            self._alive = False
            self._last_alive_check = 0.0

        return True

    def _close_serial(self) -> None:
        with self._cmd_cv:
            self._cmd_cv.notify_all()

        with self._lock:
            ser = self._ser
            self._ser = None
            was_open = self._is_open
            was_alive = self._alive
            self._is_open = False
            self._alive = False

        if ser:
            try:
                ser.close()
            except Exception:
                pass

        # Only emit LINK_DOWN if we were previously alive (avoid spam on initial failures)
        if was_open and was_alive:
            self.bus.publish(SerialEvent(SerialEventType.LINK_DOWN, time.monotonic(), "LINK_DOWN"))

    # --------------------
    # Worker loops
    # --------------------
    def _writer_loop(self) -> None:
        ser = self._ser
        if ser is None:
            self._fatal_evt.set()
            return

        next_ping = time.monotonic()

        while not self._stop_evt.is_set():
            now = time.monotonic()

            # Send PING every 1s to Arduino RX when serial is open (maintain connection)
            if now >= next_ping:
                if not self._safe_write(ser, b"PING\n"):
                    self._fatal_evt.set()
                    return
                next_ping = now + self._ping_interval_s

            try:
                out = self._tx_q.get(timeout=0.05)
            except queue.Empty:
                continue

            if not out.endswith("\n"):
                out += "\n"

            if not self._safe_write(ser, out.encode("utf-8", errors="replace")):
                self._fatal_evt.set()
                return

    def _reader_loop(self) -> None:
        ser = self._ser
        if ser is None:
            self._fatal_evt.set()
            return

        while not self._stop_evt.is_set():
            try:
                raw = ser.readline()
            except Exception as e:
                with self._lock:
                    self._last_err = f"read failed: {e}"
                self.bus.publish(SerialEvent(SerialEventType.ERROR, time.monotonic(), self._last_err))
                self._fatal_evt.set()
                return

            if not raw:
                continue

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            now = time.monotonic()
            with self._lock:
                self._last_rx_at = now
                self._rx_lines += 1

            self._route_line(line, now)
            self._maybe_emit_link_state(now)

    def _safe_write(self, ser: serial.Serial, payload: bytes) -> bool:
        try:
            ser.write(payload)
            with self._lock:
                self._last_tx_at = time.monotonic()
                self._tx_lines += 1
            return True
        except Exception as e:
            with self._lock:
                self._last_err = f"write failed: {e}"
            self.bus.publish(SerialEvent(SerialEventType.ERROR, time.monotonic(), self._last_err))
            return False

    # --------------------
    # Alive + link events
    # --------------------
    def _compute_alive_locked(self, now: float) -> bool:
        # ONLY trust Arduino heartbeat ack
        return bool(
            self._is_open
            and self._last_alive_ack_at > 0.0
            and (now - self._last_alive_ack_at) <= self._alive_timeout_s
        )

    def _maybe_emit_link_state(self, now: float) -> None:
        if now - self._last_alive_check < 0.2:
            return
        self._last_alive_check = now

        with self._lock:
            was_alive = self._alive
            alive_now = self._compute_alive_locked(now)
            self._alive = alive_now

        if alive_now and not was_alive:
            self.bus.publish(SerialEvent(SerialEventType.LINK_UP, now, "LINK_UP"))
        elif (not alive_now) and was_alive:
            self.bus.publish(SerialEvent(SerialEventType.LINK_DOWN, now, "LINK_DOWN"))

    # --------------------
    # Routing
    # --------------------
    def _route_line(self, line: str, now: float) -> None:
        # Heartbeat ACK from Arduino
        if line == "P,0,ALIVE":
            with self._lock:
                self._last_alive_ack_at = now
            # Optional: publish for observers
            self.bus.publish(SerialEvent(SerialEventType.RAW, now, raw=line))
            return

        if line.startswith("L,"):
            # L,<type>,<module>,<text>  e.g. L,0,SENS,<Sensor_name>,<value_or_rest>
            parts = line.split(",", 4)  # at most 5 parts: L, type, module, name, value...
            if len(parts) >= 4:
                typ, mod = parts[1], parts[2]
                text = parts[3] + ("," + parts[4] if len(parts) > 4 else "")
                log_ev = SerialEvent(
                    type=SerialEventType.LOG,
                    ts=now,
                    raw=line,
                    module=mod,
                    log_type=int(typ) if typ.isdigit() else None,
                    text=text,
                )
                self.bus.publish(log_ev)
                # Sensor logs: L,0,SENS,<Sensor_name>,...
                if mod == "SENS" and len(parts) >= 4:
                    sensor_name = parts[3].strip()
                    sensor_value = parts[4].strip() if len(parts) > 4 else ""
                    self.bus.publish(
                        SerialEvent(
                            type=SerialEventType.SENSOR,
                            ts=now,
                            raw=line,
                            module=mod,
                            log_type=int(typ) if typ.isdigit() else None,
                            text=text,
                            sensor_name=sensor_name or None,
                            sensor_value=sensor_value or None,
                        )
                    )
                if mod == "UI" and len(parts) >= 4:
                    component_name = parts[3].strip()
                    component_value = parts[4].strip() if len(parts) > 4 else ""
                    self.bus.publish(SerialEvent(
                        type=SerialEventType.UI,
                        ts=now,
                        raw=line,
                        module=mod,
                        log_type=int(typ) if typ.isdigit() else None,
                        text=text,
                        component_name=component_name or None,
                        component_value=component_value or None,
                    ))
            else:
                self.bus.publish(SerialEvent(SerialEventType.LOG, now, raw=line))
            return
        
        if line.startswith("R,"):
            # If a command is waiting, fulfill it (first R wins)
            with self._cmd_cv:
                if self._cmd_waiting and self._cmd_reply is None:
                    self._cmd_reply = line
                    self._cmd_cv.notify_all()

            # Still publish event for observers
            self.bus.publish(SerialEvent(SerialEventType.RSP, now, raw=line))
            return

        # if line.startswith("R,"):
        #     self.bus.publish(SerialEvent(SerialEventType.RSP, now, raw=line))
        #     return

        self.bus.publish(SerialEvent(SerialEventType.RAW, now, raw=line))
