import os
import discord
import pandas as pd
import aiohttp
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from google.transit import gtfs_realtime_pb2
from discord.ui import View, Button
import asyncio
import track


## SETUP

# 1. Ensure GTFS Static is downloaded and in the directory with the name "gtfs_static"

# 2. Ensure you have a .env file with your Discord bot token

# 3. Install required packages:
# pip install discord.py aiohttp pandas google-transit

# 4. Create a users folder


# ANSI color codes for terminal output
ansi_colours = {
    "30": (0, 0, 0),        # gray/black
    "31": (155, 0, 0),      # red
    "32": (0, 255, 0),      # green
    "33": (255, 255, 0),    # yellow/gold
    "34": (0, 0, 155),      # blue
    "35": (255, 0, 255),    # purple
    "36": (0, 255, 255),    # cyan
    "37": (255, 255, 255),  # white
}

def hex_to_rgb(hex_color: str):
    """Converts a hex color string to an (r, g, b) tuple."""
    hex_color = hex_color.strip().lstrip("#")
    if len(hex_color) != 6:
        return (0, 0, 0)  # fallback to black
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return (r, g, b)

def closest_ansi_color(hex_color: str) -> str:
    """Finds the closest ANSI color code for a given hex color."""
    target_rgb = hex_to_rgb(hex_color)

    def distance(c1, c2):
        """Calculates the Euclidean distance between two RGB colors."""
        return sum((a - b) ** 2 for a, b in zip(c1, c2))

    closest = min(ansi_colours.items(), key=lambda item: distance(target_rgb, item[1]))
    return closest[0]  # returns "31", "32", etc.


# --- Bot Code ---

# Load environment variables from .env file
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError("Missing token!")

# Path to the GTFS static data
gtfs_path = "SEQ_GTFS" # Will need a way to automatically update this.
if gtfs_path is None or not os.path.exists(gtfs_path):
    raise ValueError("GTFS static data path is not set or does not exist! Please download the latest GTFS static data from TransLink and set the path correctly. https://translink.com.au/about-translink/open-data")

# Load GTFS data into pandas DataFrames
routes = pd.read_csv(os.path.join(gtfs_path, "routes.txt"))
trips = pd.read_csv(os.path.join(gtfs_path, "trips.txt"))
stops = pd.read_csv(os.path.join(gtfs_path, "stops.txt"))
stop_times = pd.read_csv(os.path.join(gtfs_path, "stop_times.txt"))

# Create lookup dictionaries for faster data access
route_lookup = routes.set_index("route_id")[["route_short_name", "route_long_name", "route_color"]].to_dict("index")
trip_lookup = trips.set_index("trip_id")["trip_headsign"].to_dict()
stop_names = stops["stop_name"].tolist()

# Set up Discord bot intents and command prefix
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def resolve_stop_input(user_input: str):
    """Resolves user input to a stop name and a list of relevant stop IDs."""
    # Standardize input
    user_input_lower = user_input.strip().lower()

    # --- Match Finding ---
    # 1. Exact match for stop_id
    match = stops[stops["stop_id"] == user_input]
    # 2. Exact match for stop_name (case-insensitive)
    if match.empty:
        match = stops[stops["stop_name"].str.lower() == user_input_lower]
    # 3. Fuzzy match for stop_name
    if match.empty:
        fuzzy_matches = stops[stops["stop_name"].str.contains(user_input, case=False, na=False)]
        if not fuzzy_matches.empty:
            # Prefer matches that start with the input, then sort by length
            fuzzy_matches['startswith'] = fuzzy_matches['stop_name'].str.lower().str.startswith(user_input_lower)
            fuzzy_matches['length'] = fuzzy_matches['stop_name'].str.len()
            fuzzy_matches = fuzzy_matches.sort_values(['startswith', 'length'], ascending=[False, True])
            match = fuzzy_matches.head(1)

    if match.empty:
        return None, None, None

    # --- Stop Resolution ---
    stop_info = match.iloc[0]
    stop_id = stop_info["stop_id"]
    stop_name = stop_info["stop_name"]

    # Check if the matched stop is a parent station (location_type '1')
    # Or if it acts as a parent (its ID is in the 'parent_station' column of other stops)
    is_station = 'location_type' in stop_info and stop_info['location_type'] == '1'
    child_stops = stops[stops["parent_station"] == stop_id]

    if not child_stops.empty:
        # It's a parent station, return all child stop IDs
        return stop_name, child_stops["stop_id"].tolist(), True
    else:
        # It's a single stop/platform
        return stop_name, [stop_id], False

async def stop_autocomplete(interaction: discord.Interaction, current: str):
    """Provides autocomplete suggestions for stop names, prioritizing pinned stops."""
    user_id = str(interaction.user.id)
    filepath = os.path.join("users", f"{user_id}_pins.txt")
    pinned_names = []

    # Load pinned stops for the user
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            pinned_ids = [line.strip() for line in f if line.strip()]
            for pid in pinned_ids:
                match = stops[stops["stop_id"] == pid]
                if not match.empty:
                    pinned_names.append("⭐ " + match.iloc[0]["stop_name"])

    # Filter stop names based on user's input
    results = [name for name in stop_names if current.lower() in name.lower() and name not in [n.replace("⭐ ", "") for n in pinned_names]]
    
    # Combine pinned stops with other results
    combined = pinned_names + results
    combined = combined[:25] # Limit to 25 suggestions

    await interaction.response.autocomplete([
        app_commands.Choice(name=name, value=name.replace("⭐ ", "")) for name in combined
    ])

async def pid_mode_autocomplete(interaction: discord.Interaction, current: str):
    """Provides autocomplete suggestions for PID modes."""
    modes = ["General", "Rail", "Bus", "Tram"]
    return [app_commands.Choice(name=mode, value=mode) for mode in modes if current.lower() in mode.lower()]

# --- Bot Events and Commands ---

@bot.event
async def on_ready():
    """Event handler for when the bot is ready and connected to Discord."""
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)
    print(f"Logged in as {bot.user}")

@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping(interaction: discord.Interaction):
    """Responds with the bot's latency."""
    await interaction.response.send_message(f"Pong! In {round(bot.latency * 1000)}ms")


# --- Pin System ---

@bot.tree.command(name="pin", description="Pin a stop for quick access.")
@app_commands.autocomplete(stop_name=stop_autocomplete)
async def pin(interaction: discord.Interaction, stop_name: str):
    """Pins a stop for the user, saving it to a file."""
    user_id = str(interaction.user.id)
    stop_real_id, stop_real_name = resolve_stop_input(stop_name)

    if not stop_real_id:
        await interaction.response.send_message("Could not find that stop.", ephemeral=True)
        return

    directory = "users"
    if not os.path.exists(directory):
        os.makedirs(directory)

    filepath = os.path.join(directory, f"{user_id}_pins.txt")
    with open(filepath, "a+") as f:
        f.seek(0)
        if stop_real_id in [line.strip() for line in f]:
            await interaction.response.send_message(f"Stop '{stop_real_name}' is already pinned.", ephemeral=True)
        else:
            f.write(f"{stop_real_id}\n")
            await interaction.response.send_message(f"Pinned stop: '{stop_real_name}'.", ephemeral=True)

@bot.tree.command(name="view", description="View next departures for a stop.")
@app_commands.autocomplete(stop_name=stop_autocomplete, pid_mode=pid_mode_autocomplete)
@app_commands.describe(
    stop_name="The name or ID of the stop to view.",
    service_count="Number of services to display (default is 8).",
    pid_mode="The display mode for the departures. 'General', 'Rail', 'Bus', or 'Tram'."
)
async def view(interaction: discord.Interaction, stop_name: str, service_count: int = 8, pid_mode: str = "General"):
    """Fetches and displays real-time and scheduled departures for a stop or station."""
    await interaction.response.defer(thinking=True, ephemeral=False)

    # Resolve stop name to get a list of stop IDs
    stop_real_name, stop_ids, is_station = resolve_stop_input(stop_name)
    if not stop_ids:
        await interaction.followup.send(f"Could not find stop or station: '{stop_name}'", ephemeral=True)
        return

    # Auto-adjust service count for specific PID modes if not user-set
    if service_count == 8: # Default value
        if pid_mode.lower() == "rail":
            service_count = 6
        elif pid_mode.lower() == "bus":
            service_count = 7
        elif pid_mode.lower() == "bus-led":
            service_count = 5

    # Fetch services for all resolved stop IDs
    upcoming_services, _ = await track.get_next_services(stop_ids, service_count)

    if not upcoming_services:
        embed = discord.Embed(
            title=f"No upcoming departures at {stop_real_name}",
            description="There are no services scheduled for this stop at this time.",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
        return

    now = datetime.now().astimezone()
    rows = []
    
    # --- PID Mode Logic ---
    pid_mode_lower = pid_mode.lower()

    if pid_mode_lower == "general":
        header = f"ID   {'Destination':<27}Time  RT"
        for service in upcoming_services:
            ansi_code = closest_ansi_color(service.get('route_color', 'FFFFFF'))
            time_diff_minutes = int((service['eta_time'] - now).total_seconds() // 60)
            eta_diff_str = "Now" if time_diff_minutes < 1 else f"{time_diff_minutes} min"
            rt_marker = " ●" if service['is_realtime'] else " ○"
            
            plat = service.get('platform', '-')
            dest = service['destination']
            display_dest = f"({plat}) {dest}" if plat != '-' else dest

            colored_dest = f"\x1b[{ansi_code}m{display_dest[:25]:<26}\x1b[0m"
            route_id_str = f"{service['route_name']:<5}"
            time_str = f"{eta_diff_str:>6}"
            rows.append(f"{route_id_str}{colored_dest}{time_str} {rt_marker}")

    elif pid_mode_lower == "rail":
        header = f"\x1b[33;1m{now.strftime('%I:%M')}\x1b[33;0m          Next services"
        rows.append(f"\x1b[0;37;1m Service{'':<21}Platform  Departs\x1b[0m")
        for service in upcoming_services:
            ansi_code = closest_ansi_color(service.get('route_color', 'FFFFFF'))
            eta_time = service['eta_time'].strftime('%H:%M')
            
            plat = service.get('platform', '-')
            dest = service['destination'].replace(' station', '').replace('Station', '')
            
            # Truncate and pad the destination string to a fixed width
            dest_str = f"{dest[:21]:<22}"
            colored_dest = f"\x1b[{ansi_code}m{dest_str}\x1b[0m"

            platform_str = f"{plat:^8}"
            time_diff_minutes = int((service['eta_time'] - now).total_seconds() // 60)
            eta_diff_str = "Now" if time_diff_minutes < 1 else f"{time_diff_minutes} min"
            rows.append(f" {eta_time:<5} {colored_dest} {platform_str} {eta_diff_str:>6}")

    elif pid_mode_lower == "bus":
        header = f"Route  Destination{'':<20}Departs"
        for service in upcoming_services:
            ansi_code = closest_ansi_color(service.get('route_color', 'FFFFFF'))
            route_name = service['route_name']
            
            plat = service.get('platform', '-')
            dest = service['destination']
            display_dest = dest

            colored_dest = f"\x1b[{ansi_code}m{display_dest[:31]:<32}\x1b[0m"
            time_diff_minutes = int((service['eta_time'] - now).total_seconds() // 60)
            eta_diff_str = "Now" if time_diff_minutes < 1 else f"{time_diff_minutes} min"
            rows.append(f"{route_name:<6} {colored_dest} {eta_diff_str:>6}")
        rows.append("\x1b[1mVisit translink.com.au or call 13 12 30\x1b[0m")

    elif pid_mode_lower == "tram":
        header = f""
        for service in upcoming_services:
            ansi_code = closest_ansi_color(service.get('route_color', 'ffd700'))
            
            plat = service.get('platform', '-')
            dest = service['destination']
            display_dest = f"({plat}) {dest}" if plat != '-' else dest

            colored_dest = f"\x1b[{ansi_code}m{display_dest[:28]:<28}\x1b[0m"
            time_diff_minutes = int((service['eta_time'] - now).total_seconds() // 60)
            eta_diff_str = "Now" if time_diff_minutes < 1 else f"{time_diff_minutes} min"
            rows.append(f"{colored_dest} {eta_diff_str:>6}")

    else: # Fallback to general
        header = f"ID   {'Route':<27}Time  RT"
        for service in upcoming_services:
            ansi_code = closest_ansi_color(service.get('route_color', 'FFFFFF'))
            time_diff_minutes = int((service['eta_time'] - now).total_seconds() // 60)
            eta_diff_str = "Now" if time_diff_minutes < 1 else f"{time_diff_minutes} min"
            rt_marker = " ●" if service['is_realtime'] else " ○"
            colored_dest = f"\x1b[{ansi_code}m{service['destination'][:25]:<26}\x1b[0m"
            route_id_str = f"{service['route_name']:<5}"
            time_str = f"{eta_diff_str:>6}"
            rows.append(f"{route_id_str}{colored_dest}{time_str} {rt_marker}")

    # Set embed color based on the first service
    embed_color = 0x000000  # Default to black
    if upcoming_services:
        # Get color from the first service, default to black if not present
        hex_color = upcoming_services[0].get('route_color', '000000')
        # discord.py color requires an integer from hex
        embed_color = int(hex_color, 16)

    # Create and send embed
    embed = discord.Embed(
        title=f"Next Departures at {stop_real_name}",
        color=embed_color
    )
    embed.description = f"```ansi\n{header}\n" + "\n".join(rows) + "\n```"
    embed.set_footer(text=f"Last updated: {now.strftime('%H:%M:%S')} | Mode: {pid_mode.capitalize()}")
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="timetable", description="Get the full timetable for a route at a specific time.")
@app_commands.describe(route_id="Route short name (e.g. 100)", time="Time in 24h format like 15:30", direction="Trip direction (Inbound or Outbound)")
async def timetable(interaction: discord.Interaction, route_id: str, time: str = None, direction: str = "Inbound"):
    await interaction.response.defer()
    try:
        now = datetime.now()
        target_time = now
        if time:
            try:
                parsed = datetime.strptime(time, "%H:%M")
                target_time = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            except ValueError:
                await interaction.followup.send("❌ Invalid time format. Use HH:MM (24-hour).", ephemeral=True)
                return

        route_matches = routes[routes["route_short_name"].str.strip().str.lower() == route_id.strip().lower()]
        if route_matches.empty:
            await interaction.followup.send(f"❌ Route `{route_id}` not found.", ephemeral=True)
            return
        route = route_matches.iloc[0]
        route_id_actual = route["route_id"]
        route_color = route.get("route_color", "00FF00")

        route_mode_lookup = routes.set_index("route_id")["route_type"].to_dict()
        mode_code = route_mode_lookup.get(route_id_actual)
        mode_str = {
            0: "<:LightRail:1385977155913256960> Light Rail",
            1: "<:Metro:1385977091866230888> Metro", # NOTE: This will NOT show for M1, M2, RM1, RM2. As of writing, they are classified as "bus" services. This is just here for future-proofing. Only Apple seems to clasify the services as a different mode. Manual icon change is later in the code (TBD)
            2: "<:HeavyRail:1385977140448858225> Train",
            3: "<:Bus:1385976580857135184>  Bus",
            4: "<:Ferry:1385977117421994024> Ferry"
        }.get(mode_code, "❓ Unknown Mode")

        # 2. get trips for route
        trips_for_route = trips[trips["route_id"] == route_id_actual]
        direction_code = 0 if direction.lower() == "inbound" else 1
        trips_for_route = trips_for_route[trips_for_route["direction_id"] == direction_code]
        if trips_for_route.empty:
            if direction.lower() == "inbound":
                class RetryOutboundView(View):
                    def __init__(self, target_time):
                        super().__init__(timeout=60)
                        self.target_time = target_time

                    @discord.ui.button(label="Try Outbound", style=discord.ButtonStyle.danger)
                    async def try_outbound(self, interaction_button: discord.Interaction, button: discord.ui.Button):
                        await interaction_button.response.defer()  # acknowledge interaction

                        # fetch outbound trips
                        outbound_direction_code = 1
                        outbound_trips = trips[trips["route_id"] == route_id_actual]
                        outbound_trips = outbound_trips[outbound_trips["direction_id"] == outbound_direction_code]

                        if outbound_trips.empty:
                            await interaction_button.followup.send(
                                f"❌ No outbound trips found for route `{route_id}` either.",
                                ephemeral=True
                            )
                            return

                        stop_times_for_outbound = stop_times[stop_times["trip_id"].isin(outbound_trips["trip_id"])].copy()

                        def parse_arrival(time_str):
                            try:
                                h, m, s = map(int, time_str.split(":"))
                                base = now.replace(hour=0, minute=0, second=0, microsecond=0)
                                delta = timedelta(hours=h, minutes=m, seconds=s)
                                return base + delta
                            except:
                                return None

                        stop_times_for_outbound["arrival_dt"] = stop_times_for_outbound["arrival_time"].apply(parse_arrival)
                        stop_times_for_outbound = stop_times_for_outbound[stop_times_for_outbound["arrival_dt"].notnull()]
                        merged_outbound = stop_times_for_outbound.merge(outbound_trips, on="trip_id")
                        merged_outbound = merged_outbound[merged_outbound["arrival_dt"] >= self.target_time]
                        if merged_outbound.empty:
                            await interaction_button.followup.send(
                                f"❌ No scheduled outbound trips after the current time for route `{route_id}`.",
                                ephemeral=True
                            )
                            return

                        first_trip_id = merged_outbound.sort_values("arrival_dt").iloc[0]["trip_id"]
                        trip_stops = stop_times[stop_times["trip_id"] == first_trip_id].copy()
                        trip_stops["arrival_dt"] = trip_stops["arrival_time"].apply(parse_arrival)
                        trip_stops = trip_stops[trip_stops["arrival_dt"].notnull()].sort_values("arrival_dt")

                        services = []
                        for _, row in trip_stops.iterrows():
                            sid = str(row["stop_id"]).strip()
                            match = stops[stops["stop_id"].astype(str).str.strip() == sid]
                            stop_name = match["stop_name"].values[0] if not match.empty else sid
                            if len(stop_name) > 40:
                                stop_name = stop_name[:37] + "..."
                            formatted_time = row["arrival_time"][:5]
                            ansi_code = closest_ansi_color(route_color)
                            stop_fmt = f"\x1b[3;{ansi_code}m{stop_name}\x1b[0m"
                            services.append(f"\x1b[1m{formatted_time}\x1b[0m — {stop_fmt}")

                        first_stop_id = trip_stops.iloc[0]["stop_id"]
                        stop_match = stops[stops["stop_id"].astype(str).str.strip() == str(first_stop_id).strip()]
                        first_stop_name = stop_match["stop_name"].values[0] if not stop_match.empty else str(first_stop_id)

                        def hex_to_discord_color(hex_color):
                            hex_color = hex_color.strip().lstrip("#")
                            if len(hex_color) != 6:
                                return discord.Color.green()
                            r = int(hex_color[0:2], 16)
                            g = int(hex_color[2:4], 16)
                            b = int(hex_color[4:6], 16)
                            return discord.Color.from_rgb(r, g, b)

                        embed_color = hex_to_discord_color(route_color)

                        capped_services = services[:20]
                        embed = discord.Embed(
                            title=f"Next trip on Route {route_id} (Outbound) - departs {first_stop_name} at {trip_stops.iloc[0]['arrival_time']}",
                            color=embed_color
                        )
                        embed.description = "```ansi\n" + "\n".join(capped_services) + "\n```"
                        embed.set_footer(text=f"Last updated: {now.strftime('%H:%M:%S')}")

                        await interaction_button.edit_original_response(embed=embed, view=None)

                await interaction.followup.send(
                    f"❌ No inbound trips found for route `{route_id}`.",
                    view=RetryOutboundView(target_time),
                    ephemeral=True
                )
            else:
                await interaction.followup.send(f"❌ No outbound trips found for route `{route_id}`.", ephemeral=True)
            return

        stop_times_for_trips = stop_times[stop_times["trip_id"].isin(trips_for_route["trip_id"])]
        stop_times_for_trips = stop_times_for_trips.copy()

        def parse_arrival(time_str):
            try:
                h, m, s = map(int, time_str.split(":"))
                base = now.replace(hour=0, minute=0, second=0, microsecond=0)
                delta = timedelta(hours=h, minutes=m, seconds=s)
                return base + delta
            except:
                return None

        stop_times_for_trips["arrival_dt"] = stop_times_for_trips["arrival_time"].apply(parse_arrival)
        stop_times_for_trips = stop_times_for_trips[stop_times_for_trips["arrival_dt"].notnull()]

        merged = stop_times_for_trips.merge(trips_for_route, on="trip_id")
        merged = merged[merged["arrival_dt"] >= target_time]
        if merged.empty:
            await interaction.followup.send("No scheduled trips after that time.", ephemeral=True)
            return

        first_trip_id = merged.sort_values("arrival_dt").iloc[0]["trip_id"]
        trip_stops = stop_times[stop_times["trip_id"] == first_trip_id].copy()
        trip_stops["arrival_dt"] = trip_stops["arrival_time"].apply(parse_arrival)
        trip_stops = trip_stops[trip_stops["arrival_dt"].notnull()].sort_values("arrival_dt")

        services = []
        for _, row in trip_stops.iterrows():
            sid = str(row["stop_id"]).strip()
            match = stops[stops["stop_id"].astype(str).str.strip() == sid]
            stop_name = match["stop_name"].values[0] if not match.empty else sid
            if len(stop_name) > 40:
                stop_name = stop_name[:37] + "..."
            formatted_time = row['arrival_time'][:5]
            ansi_code = closest_ansi_color(route_color)
            if "platform" in stop_name.lower():
                stop_fmt = f"\x1b[1;{ansi_code}m{stop_name}\x1b[0m"  # bold + route color
            else:
                stop_fmt = f"\x1b[3;{ansi_code}m{stop_name}\x1b[0m"  # italic + route color
            services.append(f"\x1b[1m{formatted_time}\x1b[0m — {stop_fmt}")
        first_stop_id = trip_stops.iloc[0]["stop_id"]
        stop_match = stops[stops["stop_id"].astype(str).str.strip() == str(first_stop_id).strip()]
        first_stop_name = stop_match["stop_name"].values[0] if not stop_match.empty else str(first_stop_id)

        def hex_to_discord_color(hex_color):
            hex_color = hex_color.strip().lstrip("#")
            if len(hex_color) != 6:
                return discord.Color.green()
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            return discord.Color.from_rgb(r, g, b)

        embed_color = hex_to_discord_color(route_color)

        capped_services = services[:20]
        is_truncated = len(services) > 20

        embed = discord.Embed(
            title=f"Next trip on Route {route_id} - departs {first_stop_name} at {trip_stops.iloc[0]['arrival_time']}",
            color=embed_color
        )
        embed.description = (mode_str) + " Timetable\n```ansi\n" + "\n".join(capped_services) + "\n```"
        embed.set_footer(text=f"Made with ❤️")

        if is_truncated:
            class FullView(View):
                def __init__(self):
                    super().__init__(timeout=120)

                @discord.ui.button(label="Show full list", style=discord.ButtonStyle.primary)
                async def show_all(self, interaction_button: discord.Interaction, button: discord.ui.Button):
                    embed.description = "```ansi\n" + "\n".join(services) + "\n```"
                    await interaction_button.response.edit_message(embed=embed, view=None)

            await interaction.followup.send(embed=embed, view=FullView())
        else:
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}", ephemeral=True)

if __name__ == "__main__":
    # Import the track script to access its functions
    import track
    bot.run(TOKEN)