# Smart Room Manager — SPEC

## 1. Overview

**Smart Room Manager** is a self-contained room control system designed to manage room usage sessions automatically, without the need for staff intervention. Each room is equipped with:

* A **Raspberry Pi** acting as the Room Controller
* An **Arduino** acting as the hardware interface for sensors, buttons, display, and buzzer

The system supports **time-based room bookings**, **user authentication**, **environment monitoring**, and **automated feedback** (display, buzzer, lighting cues). It is designed to operate fully standalone per room, while optionally exposing APIs for centralized coordination across multiple rooms.

---

## 2. Core Design Principles

1. **Room-Local Authority**
   Each Raspberry Pi is the authoritative manager of its own room. It does not depend on a central server to function.

2. **Separation of Responsibilities**

   * Arduino: real-time I/O, sensors, UI feedback
   * Raspberry Pi: session logic, authentication, persistence, networking

3. **Event-Driven Architecture**
   No polling-based log storage. Sensor data and system events are propagated via event subscriptions.

4. **Fail-Safe & Deterministic**
   If connections drop or authentication fails, the room returns to a safe idle state.

5. **Extensible but Minimal**
   UI is local (Streamlit). External systems interact only through APIs or sockets, not the UI.

---

## 3. Physical Components

### 3.1 Arduino (Interface & I/O Layer)

Responsibilities:

* Collect sensor data
* Handle physical inputs
* Drive output devices
* Maintain a simple serial protocol

Connected hardware:

* Temperature sensor
* Air quality sensor
* Motion (PIR) sensor
* Push buttons (left/right)
* Buzzer
* RGB LCD (16x2)

Key behaviors:

* 30-second boot stabilization delay for sensors
* Serial heartbeat (`PING` / `P,0,ALIVE`)
* Stateless with respect to bookings (logic handled by Pi)

Arduino communicates **only** through serial and does not store sessions or users.

---

### 3.2 Raspberry Pi (Room Controller)

Responsibilities:

* Session lifecycle management
* User authentication
* Booking enforcement
* Database persistence
* Local UI (admin + client)
* Communication with Arduino

The Raspberry Pi runs continuously and manages the room autonomously.

---

## 4. Software Stack

### 4.1 Arduino ↔ Raspberry Pi Communication

* Transport: Serial (USB)
* Protocol: Line-based ASCII
* Managed by: `ArduinoSerialClient`

Characteristics:

* Threaded (reader + writer)
* Event-based (no log storage)
* Heartbeat-driven link detection
* Command-response validation

Arduino emits:

* Logs: `L,<type>,<module>,<text>`
* Responses: `R,0,<msg>` or `R,1,<error>`
* Heartbeat response: `P,0,ALIVE`

---

### 4.2 ArduinoSerialClient (Python)

Role:

* Sole interface to Arduino
* Runs in its own threads
* Publishes events via subscription (event bus)

Key features:

* Automatic heartbeat (`PING`)
* Link up / link down detection
* Command execution with:

  * OK → return value
  * Error → exception (`CommandError: <reason>`)
  * Timeout (5s) → exception

No Arduino logic is duplicated on the Pi.

---

### 4.3 Database (MySQL via PyMySQL)

* Runs locally on Raspberry Pi
* Managed via phpMyAdmin
* Used for persistence and auditing

No SQLite is used.

---

### 4.4 Streamlit (Local UI)

Used for:

* **Admin dashboard** (room configuration, manual overrides)
* **Client dashboard** (session status, guidance, user-facing view)

Properties:

* Runs on the Raspberry Pi but **may be embedded or linked to** from the organisation's web application (e.g. with query parameters).
* Fast iteration and built-in UI components

---

### 4.5 Socket Server (Room Runtime API)

Purpose:

* Real-time session interaction
* Authentication handshake
* Client presence tracking

Runs on Raspberry Pi and:

* Accepts local or network clients
* Validates user identity
* Coordinates session ownership

This socket server is the **runtime interface** for users.

---

### 4.6 FastAPI (Optional External API)

Used **only if** organization wants to manage multiple rooms.

Rules:

* API only (no UI)
* Exposes room state, availability, metrics
* Does not override room-local authority

---

## 5. Session Model

### 5.1 Booking Structure


An authentication session is defined by AUTH ID

A room session is defined by:

* Session ID
* Room ID (implicit per Pi)
* User ID
* Start time
* End time

Rules:

* Start time and end time are fixed
* Session may start late
* Session **always ends at end time**
* No overlap allowed

---

### 5.2 Session Lifecycle

1. Session exists in database (future)
2. At start time:

   * Room enters "reserved" state
   * Arduino displays Session ID / instructions
3. User authenticates via socket server
4. Session becomes active
5. At end time:

   * Session forcibly ends
   * Room resets to idle

Late authentication does **not** extend the session.

---

## 6. Authentication & User Model

### 6.1 Organisation-Managed User Directory

* User data is **owned by the organisation** (e.g. school web application) and persisted in the room-local MySQL database.
* Each user row includes:
  * User ID (`uid`) — organisation's canonical user identifier
  * Username — human-readable username
  * User hash — verifiable key generated by the organisation
  * Optional hash expiry timestamp

The organisation's web application is responsible for creating/updating these records.

### 6.2 Session Ownership (Organisation-Side)

* Room sessions (`session_id`, `room_id`, `user_uid`, start/end times, status) are created and managed by the organisation.
* The organisation's web app links bookings to users via `user_uid` and writes those sessions to the room-local database.
* The Pi never creates long-term user records; it only reads them.

### 6.3 User-Hash Based Authentication

Authentication is driven by an **organisation-generated hash key**:

1. Organisation web app generates a **user hash** linked to the user's data in MySQL.
2. The app exposes or embeds a URL to the Streamlit client dashboard of the form:

   * `https://room-host-or-domain:8501/?user_hash=<GENERATED_HASH>`

3. The Streamlit client dashboard:

   * Reads `user_hash` from the URL (or user input),
   * Looks up the corresponding user in the local `users` table,
   * Verifies that the hash matches and is not expired.

4. If the hash is valid:

   * The user is considered **authenticated** into the client dashboard.
   * The dashboard shows that user's **current and upcoming sessions** for this room.

5. If invalid or expired:

   * Access is denied in the dashboard.

### 6.4 Runtime Session Enforcement

* The room controller / socket server uses the database session data to enforce:
  * Which user currently "owns" the room (based on `user_uid` and time windows).
  * That room usage respects the configured start/end times.
* The organisation may optionally expose additional runtime auth tokens via the database or socket protocol, but the **primary identity link is the user hash + user record**.

---

## 7. Arduino UI Behavior

### 7.1 Boot Phase

* Display: "Starting up..."
* Duration: 30 seconds
* Sensors stabilize

---

### 7.2 Idle Phase

* Display: `STATUS: Disconnected` or `STATUS: Connected`
* Backlight enabled

---

### 7.3 Connected / Reserved

* Display shows:

  * Session ID
  * Authentication instructions

---

### 7.4 Active Session

* LCD rows may show independent scrolling texts
* Sensors actively logged
* Buttons and buzzer enabled

---

### 7.5 Alerts

* Buzzer patterns indicate warnings
* LCD backlight blinks in sync with buzzer pauses

---

## 8. Automation & Sensors

Sensors collected periodically:

* Temperature
* Air quality (value + slope)
* Motion

Usage:

* Logging
* Presence detection
* Future automation rules

---

## 9. Failure & Recovery

### Serial Link Loss

* Arduino resets to safe UI state
* Pi marks room as disconnected

### Client Disconnect

* Session continues until end time

### Pi Restart

* Sessions reloaded from database
* Arduino re-synchronized

---

## 10. Extensibility

Designed to support:

* Multiple rooms (one Pi per room)
* Central monitoring (API only)
* Additional sensors
* Additional automation rules

---

## 11. Out of Scope (For This SPEC)

* Central server implementation
* Payment systems
* Cloud dashboards
* Mobile applications

---

## 12. Summary

Smart Room Manager is a **room-first**, **automation-driven**, and **staff-free** system that integrates hardware, local control, and optional network coordination. This SPEC defines the structure, responsibilities, and boundaries required for consistent implementation across agents and iterations.
