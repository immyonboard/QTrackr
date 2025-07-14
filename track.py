import os
import pandas as pd
import aiohttp
from datetime import datetime, timezone, timedelta
from google.transit import gtfs_realtime_pb2
import asyncio

# --- Config ---
GTFS_DIR = "SEQ_GTFS"

# --- Load GTFS Data ---
print("Loading GTFS data...")
calendar = pd.read_csv(os.path.join(GTFS_DIR, 'calendar.txt'), dtype=str)
calendar_dates = pd.read_csv(os.path.join(GTFS_DIR, 'calendar_dates.txt'), dtype=str)
routes = pd.read_csv(os.path.join(GTFS_DIR, 'routes.txt'), dtype=str)
stop_times = pd.read_csv(os.path.join(GTFS_DIR, 'stop_times.txt'), dtype=str)
stops = pd.read_csv(os.path.join(GTFS_DIR, 'stops.txt'), dtype=str)
trips = pd.read_csv(os.path.join(GTFS_DIR, 'trips.txt'), dtype=str)

# Create lookups for faster access
trip_lookup = trips.set_index('trip_id')['trip_headsign'].to_dict()
route_lookup = routes.set_index('route_id').to_dict('index')
print("GTFS data loaded.")

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

def get_scheduled_departures(stop_id, now_local):
    """Gets scheduled departures for a given stop."""
    today = now_local.date()
    yesterday = today - timedelta(days=1)

    service_ids_today = get_service_ids_for_day(today)
    service_ids_yesterday = get_service_ids_for_day(yesterday)

    trips_today = trips[trips['service_id'].isin(service_ids_today)]
    trips_yesterday = trips[trips['service_id'].isin(service_ids_yesterday)]

    stop_services = stop_times[stop_times['stop_id'] == stop_id]

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
    return merged

async def get_next_services(stop_id: str, service_count: int = 8):
    """Fetches and merges scheduled and real-time data for a specified number of services."""
    # Since we are passing the ID directly, we don't need to resolve it again.
    # We'll fetch the stop_name from the stops DataFrame for the response.
    stop_info = stops[stops['stop_id'] == stop_id]
    if stop_info.empty:
        return None, f"Stop ID '{stop_id}' not found."
    stop_real_name = stop_info.iloc[0]['stop_name']

    now_local = datetime.now().astimezone()

    # 1. Get scheduled departures
    scheduled_df = get_scheduled_departures(stop_id, now_local)
    scheduled_services = {}
    for _, row in scheduled_df.iterrows():
        scheduled_services[row['trip_id']] = {
            'scheduled_time': row['arrival_dt'],
            'eta_time': row['arrival_dt'],  # Initially, ETA is the scheduled time
            'route_name': row['route_short_name'],
            'destination': row['trip_headsign'],
            'is_realtime': False,
            'route_color': row.get('route_color', 'FFFFFF') # Add route_color
        }

    # 2. Get and merge real-time data
    url = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates"
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                data = await resp.read()
                feed.ParseFromString(data)
    except Exception as e:
        print(f"Could not fetch real-time data: {e}")

    now_utc = datetime.now(timezone.utc)
    for entity in feed.entity:
        if entity.HasField('trip_update'):
            trip_id = entity.trip_update.trip.trip_id
            if trip_id in scheduled_services:
                for stu in entity.trip_update.stop_time_update:
                    if stu.stop_id == stop_id:
                        arrival_ts = stu.arrival.time if stu.HasField('arrival') else stu.departure.time
                        if arrival_ts:
                            arrival_dt_utc = datetime.fromtimestamp(arrival_ts, timezone.utc)
                            if arrival_dt_utc >= now_utc:
                                scheduled_services[trip_id]['eta_time'] = arrival_dt_utc.astimezone(now_local.tzinfo)
                                scheduled_services[trip_id]['is_realtime'] = True

    # 4. Prepare for display
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