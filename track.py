import os
import pandas as pd
import aiohttp
from datetime import datetime, timezone, timedelta
from google.transit import gtfs_realtime_pb2
import asyncio
import re
import json

# --- Config ---
GTFS_DIR = "SEQ_GTFS"
GTFS_STATIC_URL = os.getenv("GTFS_STATIC_URL", "")  # URL to the Translink SEQ GTFS static zip

# Toggle: if true, perform a GTFS static download immediately when the updater starts
DOWNLOAD_ON_BOOT = os.getenv("DownloadOnBoot", "").strip().lower() in ("true", "1", "yes", "on")

# --- GTFS Data Loading and Auto-Update ---
print("Loading GTFS data...")

# Global GTFS dataframes and lookups; initialised by load_gtfs_data()
calendar = None
calendar_dates = None
routes = None
stop_times = None
stops = None
trips = None
trip_lookup = {}
route_lookup = {}
route_short_to_ids = {}
trip_bounds_with_type = None
trip_to_route_type = {}


def update_route_short_lookup():
    """Refresh the route_short_to_ids mapping after routes is loaded/reloaded."""
    global route_short_to_ids
    route_short_to_ids = {}
    if routes is None:
        return
    for _, row in routes.iterrows():
        short = str(row.get("route_short_name", "")).strip()
        rid = row.get("route_id")
        if short and rid:
            route_short_to_ids.setdefault(short, set()).add(rid)


def load_gtfs_data():
    """Load or reload GTFS static data into global dataframes and lookups."""
    global calendar, calendar_dates, routes, stop_times, stops, trips
    global trip_lookup, route_lookup, trip_to_route_type

    try:
        cal = pd.read_csv(os.path.join(GTFS_DIR, 'calendar.txt'), dtype=str)
        cal_dates = pd.read_csv(os.path.join(GTFS_DIR, 'calendar_dates.txt'), dtype=str)
        rts = pd.read_csv(os.path.join(GTFS_DIR, 'routes.txt'), dtype=str)
        st_times = pd.read_csv(os.path.join(GTFS_DIR, 'stop_times.txt'), dtype=str)
        sts = pd.read_csv(os.path.join(GTFS_DIR, 'stops.txt'), dtype=str)
        trps = pd.read_csv(os.path.join(GTFS_DIR, 'trips.txt'), dtype=str)
    except Exception as e:
        print("error loading GTFS static data:", e)
        return

    # Swap into globals only after everything loads successfully
    calendar = cal
    calendar_dates = cal_dates
    routes = rts
    stop_times = st_times
    stops = sts
    trips = trps

    # Create lookups for faster access
    try:
        trip_lookup = trips.set_index('trip_id')['trip_headsign'].to_dict()
    except Exception:
        trip_lookup = {}
    try:
        route_lookup = routes.set_index('route_id').to_dict('index')
    except Exception:
        route_lookup = {}
    try:
        merged = trps.merge(rts[["route_id", "route_type"]], on="route_id", how="left")
        trip_to_route_type = merged.set_index("trip_id")["route_type"].astype(str).to_dict()
    except Exception:
        trip_to_route_type = {}

    update_route_short_lookup()
    build_trip_time_bounds()
    print("GTFS data loaded/reloaded.")
def get_routes_operating_today(date_obj=None):
    """Return the number of distinct routes that have at least one trip operating on the given date."""
    if date_obj is None:
        date_obj = datetime.now().astimezone().date()

    if trips is None:
        return 0

    try:
        service_ids = get_service_ids_for_day(date_obj)
    except Exception as e:
        print("warning computing routes operating today (service_ids):", e)
        return 0

    if not service_ids:
        return 0

    try:
        trips_today = trips[trips['service_id'].isin(service_ids)]
        return int(trips_today['route_id'].nunique())
    except Exception as e:
        print("warning computing routes operating today (trips_today):", e)
        return 0

async def get_rtt_vehicle_counts():
    """Return counts of active vehicles reporting RTT right now by mode using the VehiclePositions feed.

    Counts distinct active vehicles in the VehiclePositions feed, grouped by GTFS route_type:
      2 = rail, 3 = bus, 0 = tram/light rail.

    We primarily determine route_type from the trip's route_id as provided in the
    VehiclePositions feed, falling back to a trip_id -> route_type map if needed.
    """
    counts = {"trains": 0, "buses": 0, "trams": 0}
    url = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions"
    feed = gtfs_realtime_pb2.FeedMessage()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    print(f"get_rtt_vehicle_counts: HTTP {resp.status} from VehiclePositions")
                    return counts
                data = await resp.read()
                feed.ParseFromString(data)
    except Exception as e:
        print("get_rtt_vehicle_counts: could not fetch/parse VehiclePositions:", e)
        return counts

    global trip_to_route_type
    local_map = trip_to_route_type

    # If the cached trip->route_type map is empty, build a quick one on the fly
    if not local_map and trips is not None and routes is not None:
        try:
            merged = trips.merge(routes[["route_id", "route_type"]], on="route_id", how="left")
            local_map = merged.set_index("trip_id")["route_type"].astype(str).to_dict()
        except Exception as e:
            print("get_rtt_vehicle_counts: failed to build trip->route_type map on the fly:", e)
            local_map = {}

    # Pre-build a fast route_id -> route_type lookup from the routes dataframe
    route_type_by_id = {}
    if routes is not None:
        try:
            for _, row in routes.iterrows():
                rid = str(row.get("route_id", "")).strip()
                if not rid:
                    continue
                rtype = str(row.get("route_type", "")).strip()
                if rtype:
                    route_type_by_id[rid] = rtype
        except Exception as e:
            print("get_rtt_vehicle_counts: failed to build route_id->route_type map:", e)

    seen_train = set()
    seen_bus = set()
    seen_tram = set()

    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue

        vp = entity.vehicle
        tid = vp.trip.trip_id
        rid = vp.trip.route_id

        # choose a stable vehicle key so one vehicle is counted once
        vid = vp.vehicle.id or vp.vehicle.label or tid or rid
        if not vid:
            continue

        rtype = None

        # 1) Prefer route_type via route_id from the feed (more robust than trip_id)
        if rid:
            rid_str = str(rid).strip()
            rtype = route_type_by_id.get(rid_str)

        # 2) Fall back to trip_id -> route_type mapping if needed
        if not rtype and tid and local_map:
            rtype = local_map.get(str(tid))

        if not rtype:
            continue

        if rtype == "2":
            seen_train.add(vid)
        elif rtype == "3":
            seen_bus.add(vid)
        elif rtype == "0":
            seen_tram.add(vid)

    counts["trains"] = len(seen_train)
    counts["buses"] = len(seen_bus)
    counts["trams"] = len(seen_tram)
    return counts


def build_trip_time_bounds():
    """Precompute per-trip start/end seconds from midnight and attach route_type for stats."""
    global trip_bounds_with_type
    if stop_times is None or trips is None or routes is None:
        trip_bounds_with_type = None
        return

    def _time_to_seconds(time_str):
        try:
            h, m, s = map(int, str(time_str).split(":"))
            return h * 3600 + m * 60 + s
        except Exception:
            return None

    try:
        st = stop_times.copy()
        st["arr_sec"] = st["arrival_time"].apply(_time_to_seconds)
        tb = st.groupby("trip_id")["arr_sec"].agg(["min", "max"]).reset_index()
        tb = tb.rename(columns={"min": "start_sec", "max": "end_sec"})
        tb = tb.merge(trips[["trip_id", "route_id"]], on="trip_id", how="left")
        tb = tb.merge(routes[["route_id", "route_type"]], on="route_id", how="left")
        trip_bounds_with_type = tb
    except Exception as e:
        print("warning building trip time bounds:", e)
        trip_bounds_with_type = None

# Initial load at import time
load_gtfs_data()

# --- Extra statistics helpers for bot.py ---
def get_running_trip_counts(now_local=None):
    """Return approximate counts of scheduled trips currently in progress by mode.

    Uses precomputed trip_bounds_with_type and GTFS route_type values:
    2 = rail, 3 = bus, 0 = tram/light rail (all stored as strings).
    """
    from datetime import datetime as _dt

    if now_local is None:
        now_local = _dt.now().astimezone()

    trains_running = 0
    buses_running = 0
    trams_running = 0

    global trip_bounds_with_type
    if trip_bounds_with_type is None:
        return trains_running, buses_running, trams_running

    try:
        now_sec = now_local.hour * 3600 + now_local.minute * 60 + now_local.second
        active = trip_bounds_with_type[
            trip_bounds_with_type["start_sec"].notnull()
            & trip_bounds_with_type["end_sec"].notnull()
            & (trip_bounds_with_type["start_sec"] <= now_sec)
            & (trip_bounds_with_type["end_sec"] >= now_sec)
        ]
        # route_type is loaded as string in load_gtfs_data (dtype=str)
        trains_running = int((active["route_type"] == '2').sum())
        buses_running = int((active["route_type"] == '3').sum())
        trams_running = int((active["route_type"] == '0').sum())
    except Exception as e:
        print("warning computing running trip counts:", e)

    return trains_running, buses_running, trams_running


def get_route_counts_by_mode():
    """Return a dict with total route counts by mode (rail/bus/tram)."""
    counts = {"rail_routes": 0, "bus_routes": 0, "tram_routes": 0}
    if routes is None:
        return counts
    try:
        rt = routes["route_type"].astype(str)
        counts["rail_routes"] = int((rt == '2').sum())
        counts["bus_routes"] = int((rt == '3').sum())
        counts["tram_routes"] = int((rt == '0').sum())
    except Exception as e:
        print("warning computing route counts by mode:", e)
    return counts


def count_alerts_today(alerts, now_local=None):
    """Count how many GTFS-RT alerts have an active_period intersecting today.

    This is recomputed each call and effectively resets at local midnight.
    """
    from datetime import datetime as _dt

    if now_local is None:
        now_local = _dt.now().astimezone()
    today = now_local.date()

    total = 0
    for alert in alerts:
        used = False
        try:
            if getattr(alert, "active_period", None):
                for ap in alert.active_period:
                    start_dt = _dt.fromtimestamp(ap.start, timezone.utc).astimezone() if ap.start else None
                    end_dt = _dt.fromtimestamp(ap.end, timezone.utc).astimezone() if ap.end else None
                    if start_dt and end_dt:
                        if start_dt.date() <= today <= end_dt.date():
                            used = True
                            break
                    elif start_dt:
                        if start_dt.date() <= today:
                            used = True
                            break
                    elif end_dt:
                        if today <= end_dt.date():
                            used = True
                            break
        except Exception:
            pass
        if used:
            total += 1
    return total

# Rail replacement configuration (railbus routes) loaded from JSON
RAIL_REPLACEMENTS_CONFIG_PATH = "railreplacements.json"
try:
    with open(RAIL_REPLACEMENTS_CONFIG_PATH, "r") as f:
        railrepl_config = json.load(f)
except Exception as e:
    print("warning: could not load railreplacements.json:", e)
    railrepl_config = {}


async def auto_update_gtfs_daily():
    """Background task: update GTFS static from GTFS_STATIC_URL and reload data every local midnight.

    This is intended to be scheduled from a long-running process (e.g. the Discord bot)
    via: `bot.loop.create_task(track.auto_update_gtfs_daily())`.
    """
    if not GTFS_STATIC_URL:
        print("auto_update_gtfs_daily: GTFS_STATIC_URL not set; skipping automatic updates.")
        return

    async def _do_update(reason: str):
        print(f"auto_update_gtfs_daily: starting GTFS static update ({reason})...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(GTFS_STATIC_URL, timeout=120) as resp:
                    if resp.status != 200:
                        print(f"auto_update_gtfs_daily: HTTP {resp.status} when fetching GTFS static; keeping existing data.")
                        return

                    data = await resp.read()
                    tmp_dir = GTFS_DIR + "_new"
                    if os.path.exists(tmp_dir):
                        try:
                            # remove previous temp dir
                            import shutil
                            shutil.rmtree(tmp_dir)
                        except Exception as e:
                            print("auto_update_gtfs_daily: failed to remove old temp dir:", e)
                    os.makedirs(tmp_dir, exist_ok=True)

                    import zipfile
                    from io import BytesIO
                    try:
                        with zipfile.ZipFile(BytesIO(data)) as zf:
                            zf.extractall(tmp_dir)
                    except Exception as e:
                        print("auto_update_gtfs_daily: failed to extract GTFS zip:", e)
                        return

                    # Swap directories atomically where possible
                    backup_dir = GTFS_DIR + "_old"
                    try:
                        if os.path.exists(backup_dir):
                            import shutil
                            shutil.rmtree(backup_dir)
                        if os.path.exists(GTFS_DIR):
                            os.rename(GTFS_DIR, backup_dir)
                        os.rename(tmp_dir, GTFS_DIR)
                        print("auto_update_gtfs_daily: GTFS directory swapped; reloading dataframes...")
                        load_gtfs_data()
                    except Exception as e:
                        print("auto_update_gtfs_daily: failed to swap GTFS directories:", e)
        except Exception as e:
            print("auto_update_gtfs_daily: error during update:", e)

    first_run = True
    while True:
        if first_run and DOWNLOAD_ON_BOOT:
            # Perform an immediate update on startup if enabled
            await _do_update("DownloadOnBoot")
            first_run = False
            # After the immediate update, continue to schedule the next one at midnight

        now = datetime.now().astimezone()
        tomorrow = (now + timedelta(days=1)).date()
        next_midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=now.tzinfo)
        sleep_seconds = max(10, (next_midnight - now).total_seconds())
        print(f"auto_update_gtfs_daily: sleeping {int(sleep_seconds)}s until {next_midnight.isoformat()}")
        await asyncio.sleep(sleep_seconds)

        await _do_update("midnight schedule")

 # --- Helper Functions ---
def resolve_stop_input(stop_name_input):
    """Finds a stop ID and real name from a user's input."""
    stop_name_input = stop_name_input.strip().lower()
    # Simple search: find the first stop that contains the input string
    result = stops[stops['stop_name'].str.lower().str.contains(stop_name_input, case=False, na=False)]
    if not result.empty:
        return result.iloc[0]['stop_id'], result.iloc[0]['stop_name']
    return None, None

def get_service_ids_for_day(date_obj):
    """Gets all active service_ids for a given date."""
    day_name = date_obj.strftime('%A').lower()
    date_str = date_obj.strftime('%Y%m%d')

    # Get services that are supposed to run on this day of the week
    active_services = calendar[calendar[day_name] == '1']
    # Filter them to only include services within their valid date range
    active_services = active_services[
        (active_services['start_date'] <= date_str) & 
        (active_services['end_date'] >= date_str)
    ]
    service_ids = set(active_services['service_id'])

    # Handle exceptions from calendar_dates.txt
    exceptions = calendar_dates[calendar_dates['date'] == date_str]
    added = exceptions[exceptions['exception_type'] == '1']['service_id']
    service_ids.update(added)

    removed = exceptions[exceptions['exception_type'] == '2']['service_id']
    service_ids.difference_update(removed)

    return service_ids

# --- Railbus detection helper ---
def get_railbus_activity_for_date(termini_candidates, date_obj=None):
    """
    Detect which heavy-rail termini have rail replacement buses (railbuses) running
    on a given date, based purely on GTFS static.

    Args:
        termini_candidates: iterable of terminus names (e.g. ['Shorncliffe', 'Varsity Lakes', ...])
        date_obj: datetime.date; if None, uses today in local time.

    Returns:
        dict mapping terminus_name -> bool (True if railbus routes appear to operate for that terminus).
    """
    if date_obj is None:
        date_obj = datetime.now().astimezone().date()

    # Get active services for the day
    service_ids = get_service_ids_for_day(date_obj)
    if not service_ids:
        return {t: False for t in termini_candidates}

    # Limit trips to those that run today
    trips_today = trips[trips['service_id'].isin(service_ids)]

    if trips_today.empty:
        return {t: False for t in termini_candidates}

    # Join with routes to get route_long_name / route_desc / route_type
    trips_routes = trips_today.merge(routes, on='route_id', how='left', suffixes=('_trip', '_route'))

    # Prepare result dict; default no railbuses
    result = {t: False for t in termini_candidates}
    if trips_routes.empty:
        return result

    # Lowercase mapping of candidate terminus names for substring matching
    term_lower = {t: str(t).lower() for t in termini_candidates}

    # Keywords that likely indicate a rail replacement bus service
    railbus_keywords = [
        "rail replacement",
        "train replacement",
        "rail bus",
        "railbus",
        "track closure",
        "bus replacement",
    ]

    for _, row in trips_routes.iterrows():
        # Route must be a bus (route_type == 3 in Translink GTFS)
        rtype = str(row.get('route_type', '')).strip()
        if rtype and rtype != '3':
            continue

        # Build a text blob to search
        text_parts = [
            str(row.get('route_long_name', '')),
            str(row.get('route_desc', '')),
            str(row.get('trip_headsign', '')),
        ]
        blob = " ".join(text_parts).lower()

        if not any(k in blob for k in railbus_keywords):
            continue

        # If this looks like a railbus, see which termini it appears to reference
        for term, t_lower in term_lower.items():
            if t_lower and t_lower in blob:
                result[term] = True

    return result

def get_rail_replacement_status_for_date(date_obj=None, lookahead_days: int = 7):
    """
    Use railreplacements.json + GTFS static to determine which termini have rail
    replacement bus routes (railbuses) running today (current) and within the
    next `lookahead_days` days (upcoming).

    Returns:
        current_termini: set[str]
        upcoming_termini: set[str]
        reasons_current: dict[str, list[str]]
        reasons_upcoming: dict[str, list[str]]
    """
    if date_obj is None:
        date_obj = datetime.now().astimezone().date()

    today = date_obj
    end_date = today + timedelta(days=lookahead_days)

    current_termini = set()
    upcoming_termini = set()
    reasons_current: dict[str, list[str]] = {}
    reasons_upcoming: dict[str, list[str]] = {}

    if not railrepl_config:
        return current_termini, upcoming_termini, reasons_current, reasons_upcoming

    # Step 1: normalise railrepl_config into a flat list of (terminus, short_name, desc)
    config_entries: list[tuple[str, str, str]] = []

    for key, value in railrepl_config.items():
        if key == "ClosureEmoji":
            continue

        # Shape A: route-keyed, with explicit termini list
        # "R770": { "termini": ["Beenleigh", "Varsity Lakes"], "description": "..." }
        if isinstance(value, dict) and ("termini" in value or "terminus" in value):
            short_name = key
            termini_list = value.get("termini") or value.get("terminus") or []
            if isinstance(termini_list, str):
                termini_list = [termini_list]
            desc = value.get("description") or value.get("desc") or ""
            for term in termini_list:
                term_str = str(term).strip()
                if not term_str:
                    continue
                config_entries.append((term_str, short_name, desc))
            continue

        # Shape B: terminus-keyed, mapping short_name -> desc
        # "Shorncliffe": { "R529": "No trains ..." }
        if isinstance(value, dict):
            term = str(key).strip()
            if not term:
                continue
            for short_name, desc in value.items():
                if short_name == "ClosureEmoji":
                    continue
                short_name_str = str(short_name).strip()
                if not short_name_str:
                    continue
                desc_str = str(desc).strip() if desc is not None else ""
                config_entries.append((term, short_name_str, desc_str))
            continue

        # Any other shape is ignored (e.g. raw string at top level without termini)
        # because we don't know which terminus it applies to.

    if not config_entries:
        return current_termini, upcoming_termini, reasons_current, reasons_upcoming

    # Step 2: Precompute active services for each date in range
    dates_list = [today + timedelta(days=i) for i in range(lookahead_days + 1)]
    services_by_date = {d: get_service_ids_for_day(d) for d in dates_list}

    # Step 3: For each (terminus, short_name, desc) entry, look for trips on those dates
    for term, short_name, desc in config_entries:
        route_ids = route_short_to_ids.get(short_name, set())
        if not route_ids:
            # Helpful debug for misconfigured short names
            print(f"railrepl warning: no GTFS route_short_name '{short_name}' found for terminus '{term}'")
            continue

        for d in dates_list:
            service_ids = services_by_date.get(d, set())
            if not service_ids:
                continue

            trips_for_route_day = trips[
                trips['route_id'].isin(route_ids) &
                trips['service_id'].isin(service_ids)
            ]

            if trips_for_route_day.empty:
                continue

            # At least one trip for this replacement route runs on this date
            reason_text = desc or f"Rail replacement route {short_name} operates on this date."

            if d == today:
                current_termini.add(term)
                reasons_current.setdefault(term, []).append(reason_text)
            elif today < d <= end_date:
                upcoming_termini.add(term)
                reasons_upcoming.setdefault(term, []).append(reason_text)

    return current_termini, upcoming_termini, reasons_current, reasons_upcoming

def get_scheduled_departures(stop_ids, now_local):
    """Gets scheduled departures for a given list of stop IDs."""
    today = now_local.date()
    yesterday = today - timedelta(days=1)

    service_ids_today = get_service_ids_for_day(today)
    service_ids_yesterday = get_service_ids_for_day(yesterday)

    trips_today = trips[trips['service_id'].isin(service_ids_today)]
    trips_yesterday = trips[trips['service_id'].isin(service_ids_yesterday)]

    # Filter stop_times for all stop_ids in the provided list
    stop_services = stop_times[stop_times['stop_id'].isin(stop_ids)]

    def parse_arrival(time_str, day_start):
        try:
            h, m, s = map(int, time_str.split(':'))
            return day_start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=h, minutes=m, seconds=s)
        except ValueError:
            return None

    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start_local = today_start_local - timedelta(days=1)

    services_today_df = stop_services.merge(trips_today, on='trip_id')
    services_today_df['arrival_dt'] = services_today_df['arrival_time'].apply(parse_arrival, args=(today_start_local,))

    services_yesterday_df = stop_services.merge(trips_yesterday, on='trip_id')
    services_yesterday_df['arrival_dt'] = services_yesterday_df['arrival_time'].apply(parse_arrival, args=(yesterday_start_local,))

    all_services = pd.concat([services_yesterday_df, services_today_df])
    all_services.dropna(subset=['arrival_dt'], inplace=True)
    future_services = all_services[all_services['arrival_dt'] >= now_local].copy()

    merged = future_services.merge(routes, on='route_id')
    merged_with_stops = merged.merge(stops[['stop_id', 'stop_name']], on='stop_id')
    
    # Extract platform number or letter from stop_name
    def get_platform(name):
        # Regex to find 'Platform X', 'Stop Y', or a number at the end of the string
        match = re.search(r'(?:Platform|Stop)\s*(\w+)|(\d+)$', name)
        if match:
            # Return the first non-None group from the match
            return next((g for g in match.groups() if g is not None), '-')
        return '-'

    merged_with_stops['platform'] = merged_with_stops['stop_name'].apply(get_platform)
    
    return merged_with_stops

async def get_next_services(stop_ids: list[str], service_count: int = 8):
    """Fetches and merges scheduled and real-time data for a list of stop IDs."""
    if not stop_ids:
        return None, "No stop IDs provided."

    # The stop_real_name is now resolved in the bot, so we don't need to do it here.
    # We can use the first stop_id to get a name for logging if needed, but the primary display name is handled by the caller.
    stop_info = stops[stops['stop_id'] == stop_ids[0]]
    stop_real_name = stop_info.iloc[0]['stop_name'] if not stop_info.empty else "Unknown Stop"

    now_local = datetime.now().astimezone()

    # 1. Get scheduled departures for all stop_ids
    scheduled_df = get_scheduled_departures(stop_ids, now_local)
    scheduled_services = {}
    for _, row in scheduled_df.iterrows():
        # Use a unique key combining trip_id and stop_id to handle the same trip across multiple platforms
        service_key = f"{row['trip_id']}-{row['stop_id']}"
        scheduled_services[service_key] = {
            'scheduled_time': row['arrival_dt'],
            'eta_time': row['arrival_dt'],
            'route_name': row['route_short_name'],
            'destination': row['trip_headsign'],
            'is_realtime': False,
            'route_color': row.get('route_color', 'FFFFFF'),
            'platform': row.get('platform', '-') # Get platform info
        }

    # 2. Get and merge real-time data
    url = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates"
    feed = gtfs_realtime_pb2.FeedMessage()
    updates_found = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    feed.ParseFromString(data)
                    print(f"Successfully fetched and parsed real-time data. {len(feed.entity)} entities found.")
                else:
                    print(f"Failed to fetch real-time data. Status: {resp.status}")
    except Exception as e:
        print(f"Could not fetch or parse real-time data: {e}")

    now_utc = datetime.now(timezone.utc)
    for entity in feed.entity:
        if entity.HasField('trip_update'):
            trip_id = entity.trip_update.trip.trip_id
            for stu in entity.trip_update.stop_time_update:
                service_key = f"{trip_id}-{stu.stop_id}"
                if service_key in scheduled_services:
                    arrival_ts = stu.arrival.time if stu.HasField('arrival') else stu.departure.time
                    if arrival_ts:
                        arrival_dt_utc = datetime.fromtimestamp(arrival_ts, timezone.utc)
                        if arrival_dt_utc >= now_utc:
                            scheduled_services[service_key]['eta_time'] = arrival_dt_utc.astimezone(now_local.tzinfo)
                            scheduled_services[service_key]['is_realtime'] = True
                            updates_found += 1
    
    print(f"Matched {updates_found} real-time updates to scheduled services.")

    # 4. Prepare for display
    # Since a trip might appear multiple times if it stops at multiple platforms in the list,
    # we need to decide how to handle it. For now, we'll just combine and sort them.
    upcoming = sorted(list(scheduled_services.values()), key=lambda x: x['eta_time'])
    return upcoming[:service_count], stop_real_name

# --- Main Execution ---
async def main():
    stop_id = "3000110"
    service_count = 10
    upcoming_services, stop_name = await get_next_services(stop_id, service_count)

    if not upcoming_services:
        print(stop_name) # Print error message
        return

    print(f"--- Next {service_count} Departures at {stop_name} ---")
    now = datetime.now().astimezone()
    for service in upcoming_services:
        rt_marker = "[RT]" if service['is_realtime'] else "    "
        eta_time_str = service['eta_time'].strftime("%H:%M")
        time_diff_minutes = int((service['eta_time'] - now).total_seconds() // 60)
        eta_diff_str = f"{time_diff_minutes} mins"

        print(f"{rt_marker} {service['route_name']:<4} to {service['destination']:<35} | {eta_time_str} ({eta_diff_str})")

if __name__ == "__main__":
    asyncio.run(main())