from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional
import socketio

import streamlit as st

from config import MySqlConfig
from env import MYSQL_HOST, MYSQL_USER, MYSQL_PASS, MYSQL_DB
from storage.db import Database, ensure_schema
from storage.repo import Repo


def build_db_and_repo() -> tuple[Database, Repo]:
    """Initializes database connection and repository."""
    mysql = MySqlConfig(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
    )
    db = Database(mysql)
    ensure_schema(db)
    repo = Repo(db)
    return db, repo


def admin_dashboard(repo: Repo) -> None:
    st.header("Admin Dashboard")

    # 1. Fetch all available rooms
    rooms_data = repo._db.fetch_all("SELECT room_id, room_name FROM room_info")
    
    if not rooms_data:
        st.warning("No rooms registered in the database. Ensure room servers are initialized.")
        return

    # Map: "Room Name (ID)" -> ID
    room_options = {f"{r['room_name']} ({r['room_id']})": r['room_id'] for r in rooms_data}
    selected_label = st.selectbox("Select Room to Monitor", options=list(room_options.keys()))
    selected_room_id = room_options[selected_label]

    # --- ALERT LOGIC (Lockdown Mode) ---
    critical_alert = False
    warning_alert = False

    # Fetch threshold and latest reading
    room_data = repo._db.fetch_one("SELECT max_temp FROM room_info WHERE room_id = %s", (selected_room_id,))
    latest_log = repo._db.fetch_one(
        "SELECT temp, air_quality FROM room_sensor_logs WHERE room_id = %s ORDER BY logged_at DESC LIMIT 1",
        (selected_room_id,)
    )
    
    # 1. Temperature Check
    if room_data and room_data['max_temp'] and latest_log:
        if float(latest_log['temp']) > float(room_data['max_temp']):
            critical_alert = True
            st.error(f"🚨 **CRITICAL TEMP ALERT**: Current Temperature ({latest_log['temp']}°C) exceeds maximum limit ({room_data['max_temp']}°C)!")
            st.toast("Check room ventilation immediately!", icon="⚠️")
    
    # 2. Air Quality Check
    if latest_log:
        aqi_status = latest_log.get('air_quality', 'fresh_air').lower()
        if aqi_status in ['high_pollution', 'force_impulse']:
            critical_alert = True
            st.error(f"☣️ **DANGER: {aqi_status.replace('_', ' ').upper()}**")
            st.toast("Evacuate personnel from area immediately!", icon="🚨")
        elif aqi_status == 'low_pollution':
            warning_alert = True
            # Low pollution is a warning, not a full lockdown
            st.warning("⚠️ **POOR AIR QUALITY**: Area might be unfit for long-term use.")

    # --- LOCKDOWN EXECUTION ---
    if critical_alert:
        # If critical, we PAUSE to let the user see the alert, then RERUN immediately.
        # This prevents the rest of the dashboard (tables, logs) from loading.
        time.sleep(1)
        st.rerun()
    # --------------------------

    st.markdown(f"### Monitoring: `{selected_room_id}`")
    
    # --- STANDARD DASHBOARD (Only renders if no critical alert) ---
    col1, col2 = st.columns([1, 1])

    with col1:
        st.subheader("Upcoming Sessions")
        now = datetime.now()
        upcoming = repo.get_upcoming_sessions(selected_room_id, now)
        if not upcoming:
            st.info("No upcoming sessions for this room.")
        else:
            st.table([{
                "Session ID": s.session_id,
                "User UID": s.user_uid,
                "Start": s.start_ts.strftime("%H:%M"),
                "End": s.end_ts.strftime("%H:%M"),
                "Status": s.status,
            } for s in upcoming])

    with col2:
        st.subheader("Live Sensor Logs")
        logs = repo._db.fetch_all(
            """
            SELECT logged_at, temp, air_quality, motion_detected 
            FROM room_sensor_logs 
            WHERE room_id = %s 
            ORDER BY logged_at DESC 
            LIMIT 15
            """, 
            (selected_room_id,)
        )

        if not logs:
            st.info("No sensor logs found for this room.")
        else:
            formatted_logs = []
            for l in logs:
                formatted_logs.append({
                    "Logged At": l['logged_at'].strftime("%d/%m/%Y %H:%M:%S"),
                    "Temperature": f"{l['temp']}°C",
                    "Air Quality": l['air_quality'].replace('_', ' ').title(),
                    "Motion": "Yes" if l['motion_detected'] else "No"
                })
            st.dataframe(formatted_logs, use_container_width=True)
            st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')}")

    # Standard Refresh Rate (Slow)
    time.sleep(1 if warning_alert else 10)
    st.rerun()


def client_dashboard(repo: Repo) -> None:
    st.header("Client Access")
    now = datetime.now()

    # 1. Room Selection
    rooms_data = repo._db.fetch_all("SELECT room_id, room_name, url, room_secret FROM room_info")
    if not rooms_data:
        st.error("System offline: No rooms configured.")
        return

    # Map name -> room object
    room_map = {f"{r['room_name']} ({r['room_id']})": r for r in rooms_data}
    
    try:
        query_params = st.query_params
        default_key = next((k for k, v in room_map.items() if v['room_id'] == query_params.get("room_id")), list(room_map.keys())[0])
    except:
        default_key = list(room_map.keys())[0]

    selected_label = st.selectbox("Current Room", options=list(room_map.keys()), 
                                    index=list(room_map.keys()).index(default_key))
    
    selected_room = room_map[selected_label]
    selected_room_id = selected_room['room_id']

    # -----------------------------
    # Authentication
    # -----------------------------
    st.subheader("Authenticate")
    user_hash = st.text_input("User Hash", type="password")

    if "auth_user" not in st.session_state:
        st.session_state["auth_user"] = None

    if st.button("Authenticate"):
        if not user_hash:
            st.error("Please provide your User Hash.")
        else:
            user = repo.get_user_by_hash(user_hash, now=now)
            if user is None:
                st.error("Authentication failed. Invalid hash.")
            else:
                # Check active or upcoming sessions
                room_session = repo.get_current_room_session(selected_room_id, now)
                upcoming = repo.get_upcoming_sessions(selected_room_id, now)
                next_s = upcoming[0] if upcoming else None
                
                has_active = (room_session and room_session.user_uid == user.uid)
                has_upcoming = (next_s and next_s.user_uid == user.uid and next_s.status == 'scheduled')

                if has_active or has_upcoming:
                    st.session_state["auth_user"] = user
                    st.success(f"Welcome, {user.username}!")
                else:
                    st.error("No active or upcoming session for you in this room right now.")

    user = st.session_state.get("auth_user")
    if user is None:
        return

    st.markdown("---")
    
    # -----------------------------
    # Authenticated View
    # -----------------------------
    current = repo.get_current_room_session(selected_room_id, now)
    
    # Determine which session to focus on (Active or Scheduled)
    if not current:
         upcoming = repo.get_upcoming_sessions(selected_room_id, now)
         target_session = upcoming[0] if upcoming else None
    else:
         target_session = current

    if target_session and target_session.user_uid == user.uid:
        st.subheader("Session Status")
        m1, m2, m3 = st.columns(3)
        m1.metric("Session ID", target_session.session_id)
        
        # Get latest room environment data
        latest = repo._db.fetch_one(
            "SELECT temp, air_quality FROM room_sensor_logs WHERE room_id = %s ORDER BY logged_at DESC LIMIT 1",
            (selected_room_id,)
        )
        if latest:
            m2.metric("Temperature", f"{latest['temp']} °C")
            m3.metric("Air Quality", latest['air_quality'].replace('_', ' ').title())
            
        st.write(f"Session ends at: {target_session.end_ts}")
        
        # --- SOCKET CONNECTION & VERIFICATION ---
        if "sio" not in st.session_state:
            sio_client = socketio.Client()
            try:
                # Use URL and Secret from DB
                room_url = selected_room.get('url', 'http://raspberrypi.local:5000')
                room_secret = selected_room.get('room_secret', '')
                
                # Connect with Auth Token
                sio_client.connect(room_url, auth={"token": room_secret})
                st.session_state["sio"] = sio_client
            except Exception as e:
                st.error(f"Could not connect to Room Server at {room_url}: {e}")

        sio = st.session_state.get("sio")

        # Verification Logic
        if target_session.status == "scheduled":
            st.warning("⚠️ Action Required: Verify your presence.")
            
            if st.button("I am present in the room"):
                if sio:
                    sio.emit("start_presence_verification", {"user_uid": user.uid})
                    st.info("Now, press the LEFT button on the room controller to generate your code.")

            st.markdown("> Please enter 4-digit verification code")
            ui.input_otp(max_length=4, key="code_input")
            
            if st.button("Confirm Verification"):
                if sio:
                    sio.emit("verify_code", {"code": st.session_state['code_input']})
                    st.success("Verification sent. Please wait...")
                    time.sleep(2)
                    st.rerun()

    else:
        st.warning("Your session has ended or is not valid for this room.")
        st.session_state["auth_user"] = None


def main() -> None:
    st.set_page_config(page_title="Smart Room Dashboard", layout="wide")

    db, repo = build_db_and_repo()

    view = st.sidebar.radio("View Mode", ["Client", "Admin"])

    try:
        if view == "Admin":
            admin_dashboard(repo)
        else:
            client_dashboard(repo)
    finally:
        db.close()


if __name__ == "__main__":
    main()