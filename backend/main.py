"""LadleFlow: SMS ladle scheduling simulation API.

Simulation rules and initial conditions are carried over from the supplied Colab notebook.
The scheduler starts automatically when the FastAPI application starts.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque
import heapq
import itertools
import threading
import pandas as pd
import time
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque
import pandas as pd
import heapq
import time
import threading
TZ = ZoneInfo("Asia/Kolkata")
REFRESH_SECONDS = 1

LADLE_IDS = ["L1", "L2", "L3", "L4", "L5", "L6"]

ROUTE = ["BOF", "LRF", "VD", "Caster", "Inspection"]

PROCESS_TIME = {
    "BOF": 3,
    "LRF": 4,
    "VD": 4,
    "Caster": 6,       # P95
    "Inspection": 2
}

TRANSFER_TIME = {
    ("Stand", "BOF"): 2,
    ("BOF", "LRF"): 2,
    ("LRF", "VD"): 1,
    ("VD", "Caster"): 1,
    ("Caster", "Inspection"): 1,
    ("Inspection", "Stand"): 3
}

STAND_CAPACITY = 1
PLANNING_HORIZON_MIN = 40

# PROVISIONAL: replace with actual tap-to-tap interval.
HEAT_INTERVAL_MIN = 5

INITIAL_STATE = {
    "L1": {"location": "BOF", "status": "PROCESSING", "remaining_min": 3},
    "L2": {"location": "LRF", "status": "PROCESSING", "remaining_min": 4},
    "L3": {"location": "VD", "status": "PROCESSING", "remaining_min": 4},
    "L4": {"location": "Caster", "status": "PROCESSING", "remaining_min": 5},
    "L5": {"location": "Inspection", "status": "PROCESSING", "remaining_min": 2},
    "L6": {"location": "Stand", "status": "READY", "remaining_min": 0}
}

print("Cell 1 loaded.")

# CELL 2: REAL-TIME SMS LADLE SIMULATION ENGINE

import heapq
import itertools
import threading
from collections import deque
from datetime import datetime, timedelta

def sim_now():
    return datetime.now(TZ).replace(tzinfo=None)

state_lock = threading.RLock()
events = []
event_log = []
event_counter = itertools.count()

state = {}
station_occupant = {}
station_queues = {station: deque() for station in ROUTE}
station_queues["Stand"] = deque()
stand_occupant = None

# Provisional until actual plant heat interval is confirmed.
next_heat_due = sim_now()


def clock_text(value):
    if value is None:
        return "—"
    return value.strftime("%I:%M:%S %p").lstrip("0")


def countdown_text(target, now):
    if target is None:
        return "Waiting"

    seconds_left = max(0, int((target - now).total_seconds()))
    minutes, seconds = divmod(seconds_left, 60)
    return f"{minutes:02d}:{seconds:02d}"


def schedule_event(when, event_type, lid, details):
    heapq.heappush(
        events,
        (when, next(event_counter), event_type, lid, details)
    )


def log_event(when, lid, location, status, event):
    event_log.append({
        "Time": clock_text(when),
        "Ladle": lid,
        "Location / Route": location,
        "Status": status,
        "Event": event
    })


def station_is_free(station):
    if station == "Stand":
        return stand_occupant is None
    return station not in station_occupant


def start_process(lid, station, now):
    s = state[lid]
    duration = s.get("remaining_min")

    if duration is None:
        duration = PROCESS_TIME[station]

    s["location"] = station
    s["status"] = "PROCESSING"
    s["from_station"] = None
    s["to_station"] = None
    s["available_time"] = None
    s["remaining_min"] = None
    s["current_process_end"] = now + timedelta(minutes=duration)

    station_occupant[station] = lid

    schedule_event(
        s["current_process_end"],
        "PROCESS_COMPLETE",
        lid,
        station
    )

    log_event(
        now, lid, station, "PROCESSING",
        f"Process started; estimated finish {clock_text(s['current_process_end'])}"
    )


def promote_station_queue(station, now):
    while station_queues[station] and station_is_free(station):
        lid = station_queues[station].popleft()

        if state[lid]["status"] == "WAITING":
            start_process(lid, station, now)


def promote_stand_queue(now):
    global stand_occupant

    if stand_occupant is not None:
        return

    while station_queues["Stand"]:
        lid = station_queues["Stand"].popleft()
        s = state[lid]

        if s["status"] != "WAITING_FOR_STAND":
            continue

        stand_occupant = lid
        s["location"] = "Stand"
        s["status"] = "READY"
        s["available_time"] = now
        s["current_process_end"] = None
        s["from_station"] = None
        s["to_station"] = None
        s["remaining_min"] = None

        log_event(now, lid, "Stand", "READY", "Available now")
        return


def begin_transfer(lid, source, destination, now):
    s = state[lid]
    duration = TRANSFER_TIME[(source, destination)]
    arrival = now + timedelta(minutes=duration)

    s["location"] = f"{source} → {destination}"
    s["status"] = "IN TRANSIT"
    s["from_station"] = source
    s["to_station"] = destination
    s["current_process_end"] = None
    s["available_time"] = arrival

    schedule_event(
        arrival,
        "TRANSFER_COMPLETE",
        lid,
        (source, destination)
    )

    log_event(
        now, lid, f"{source} → {destination}", "IN TRANSIT",
        f"Transfer started; arrival {clock_text(arrival)}"
    )


def handle_process_complete(event_time, lid, station):
    s = state[lid]

    if (
        s["status"] != "PROCESSING"
        or s["location"] != station
        or s["current_process_end"] != event_time
    ):
        return

    if station_occupant.get(station) == lid:
        del station_occupant[station]

    s["current_process_end"] = None

    log_event(event_time, lid, station, "COMPLETED", "Process completed")
    promote_station_queue(station, event_time)

    idx = ROUTE.index(station)
    destination = ROUTE[idx + 1] if idx + 1 < len(ROUTE) else "Stand"

    begin_transfer(lid, station, destination, event_time)


def handle_transfer_complete(event_time, lid, route):
    global stand_occupant

    s = state[lid]
    source, destination = route

    if (
        s["status"] != "IN TRANSIT"
        or s["from_station"] != source
        or s["to_station"] != destination
        or s["available_time"] != event_time
    ):
        return

    s["from_station"] = None
    s["to_station"] = None
    s["available_time"] = None

    log_event(
        event_time, lid, f"{source} → {destination}",
        "ARRIVED", "Transfer completed"
    )

    if source == "Stand" and destination == "BOF":
        if stand_occupant == lid:
            stand_occupant = None

        # Free Stand immediately and promote the next waiting ladle.
        promote_stand_queue(event_time)

        s["location"] = "BOF"

        if station_is_free("BOF"):
            start_process(lid, "BOF", event_time)
        else:
            s["status"] = "WAITING"
            s["remaining_min"] = PROCESS_TIME["BOF"]
            station_queues["BOF"].append(lid)

            log_event(
                event_time, lid, "BOF", "WAITING",
                "Waiting for BOF station"
            )

        return

    if destination == "Stand":
        s["location"] = "Stand"

        if stand_occupant is None:
            stand_occupant = lid
            s["status"] = "READY"
            s["available_time"] = event_time
            s["remaining_min"] = None

            log_event(
                event_time, lid, "Stand", "READY", "Available now"
            )
        else:
            s["status"] = "WAITING_FOR_STAND"
            s["remaining_min"] = None
            station_queues["Stand"].append(lid)

            log_event(
                event_time, lid, "Stand", "WAITING_FOR_STAND",
                "Waiting for Stand capacity"
            )

        return

    s["location"] = destination

    if station_is_free(destination):
        start_process(lid, destination, event_time)
    else:
        s["status"] = "WAITING"
        s["remaining_min"] = PROCESS_TIME[destination]
        station_queues[destination].append(lid)

        log_event(
            event_time, lid, destination, "WAITING",
            f"Waiting for {destination} station"
        )


def automatic_dispatch(now):
    global next_heat_due

    # Recover Stand occupancy from its queue if Stand is free.
    if stand_occupant is None:
        promote_stand_queue(now)

    if stand_occupant is None:
        return

    lid = stand_occupant

    if state[lid]["status"] != "READY":
        return

    if not station_is_free("BOF"):
        return

    if now < next_heat_due:
        return

    begin_transfer(lid, "Stand", "BOF", now)
    next_heat_due = now + timedelta(minutes=HEAT_INTERVAL_MIN)


def initialize_simulation():
    global stand_occupant, next_heat_due

    events.clear()
    event_log.clear()
    station_occupant.clear()

    for queue in station_queues.values():
        queue.clear()

    state.clear()
    stand_occupant = None

    now = sim_now()
    next_heat_due = now

    for lid in LADLE_IDS:
        initial = INITIAL_STATE[lid]

        state[lid] = {
            "location": initial["location"],
            "status": initial["status"],
            "from_station": None,
            "to_station": None,
            "available_time": None,
            "current_process_end": None,
            "remaining_min": initial["remaining_min"]
        }

    for lid in LADLE_IDS:
        s = state[lid]
        location = s["location"]

        if s["status"] == "PROCESSING":
            if station_is_free(location):
                start_process(lid, location, now)
            else:
                s["status"] = "WAITING"
                station_queues[location].append(lid)

                log_event(
                    now, lid, location, "WAITING",
                    "Waiting for station"
                )

        elif location == "Stand" and s["status"] == "READY":
            if stand_occupant is None:
                stand_occupant = lid
                s["available_time"] = now

                log_event(
                    now, lid, "Stand", "READY", "Initial state"
                )
            else:
                s["status"] = "WAITING_FOR_STAND"
                station_queues["Stand"].append(lid)

                log_event(
                    now, lid, "Stand", "WAITING_FOR_STAND",
                    "Waiting for Stand capacity"
                )

    log_event(now, "SYSTEM", "SMS", "RUNNING", "Simulation initialized")


initialize_simulation()



def make_live_table(now):
    rows = []

    with state_lock:
        for lid in LADLE_IDS:
            s = state[lid]
            status = s["status"]
            location = s["location"]

            if status == "PROCESSING":
                target = s["current_process_end"]
                next_event = f"Process ends {clock_text(target)}"
                availability = countdown_text(target, now)

            elif status == "IN TRANSIT":
                target = s["available_time"]
                next_event = (
                    f"Arrival at {s['to_station']}: {clock_text(target)}"
                )
                availability = countdown_text(target, now)

            elif status == "READY":
                next_event = "Available now"
                availability = "00:00"

            elif status == "WAITING_FOR_STAND":
                next_event = "Waiting for Stand capacity"
                availability = "Waiting"

            elif status == "WAITING":
                next_event = f"Waiting for {location} station"
                availability = "Waiting"

            else:
                next_event = "—"
                availability = "—"

            rows.append({
                "Ladle": lid,
                "Current Location / Route": location,
                "Status": status,
                "Next Event": next_event,
                "Ladle Availability (min:sec)": availability
            })

    return pd.DataFrame(rows)

def make_recent_events():
    columns = ["Time", "Ladle", "Location / Route", "Status", "Event"]

    with state_lock:
        rows = event_log[-8:].copy()

    return pd.DataFrame(rows, columns=columns)


app = FastAPI(title="Real-Time Ladle Management API")

frontend_origin = os.getenv("FRONTEND_ORIGIN", "").strip()
allowed_origins = [
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "https://ladle-flow-smart-ladle-scheduling-system-13f5.onrender.com",
]
if frontend_origin:
    allowed_origins.append(frontend_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {
        "message": "Ladle Management API is running",
        "status": "success"
    }


@app.get("/simulation/check")
def simulation_check():
    # Read the existing simulation variables without modifying them
    with state_lock:
        return {
            "api_status": "running",
            "simulation_variables": {
                "station_occupant": str(station_occupant),
                "station_queues": str(station_queues),
                "event_count": len(events),
                "recent_event_count": len(event_log)
            },
            "checked_at": sim_now().isoformat()
        }


@app.get("/api/live")
def get_live_data():
    try:
        now = sim_now()

        # Reuse your existing dashboard data functions
        ladles = make_live_table(now)
        recent_events = make_recent_events()

        return {
            "current_time": clock_text(now),
            "next_heat_due": clock_text(next_heat_due),
            "ladles": ladles.to_dict(orient="records"),
            "recent_events": recent_events.to_dict(orient="records")
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read simulation data: {str(e)}"
        )


def scheduler_loop(stop_event):
    while not stop_event.is_set():
        now = sim_now()
        with state_lock:
            while events and events[0][0] <= now:
                event_time, _, event_type, lid, details = heapq.heappop(events)
                if event_type == "PROCESS_COMPLETE":
                    handle_process_complete(event_time, lid, details)
                elif event_type == "TRANSFER_COMPLETE":
                    handle_transfer_complete(event_time, lid, details)
            automatic_dispatch(now)
        stop_event.wait(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app):
    stop_event = threading.Event()
    thread = threading.Thread(
        target=scheduler_loop,
        args=(stop_event,),
        daemon=True,
        name="LadleScheduler",
    )
    thread.start()
    app.state.scheduler_stop_event = stop_event
    app.state.scheduler_thread = thread
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=5)


app.router.lifespan_context = lifespan
