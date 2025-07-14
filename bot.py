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


## SETUP

# 1. Ensure GTFS Static is downloaded and in the directory with the name "gtfs_static"

# 2. Ensure you have a .env file with your Discord bot token

# 3. Install required packages:
# pip install discord.py aiohttp pandas google-transit

# 4. Create a users folder


# Colours
Keep = "\033[0;"

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
    hex_color = hex_color.strip().lstrip("#")
    if len(hex_color) != 6:
        return (0, 0, 0)  # fallback to black
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return (r, g, b)

def closest_ansi_color(hex_color: str) -> str:
    target_rgb = hex_to_rgb(hex_color)

    def distance(c1, c2):
        return sum((a - b) ** 2 for a, b in zip(c1, c2))

    closest = min(ansi_colours.items(), key=lambda item: distance(target_rgb, item[1]))
    return closest[0]  # returns "31", "32", etc.


# Code

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError("Missing token!")

gtfs_path = "SEQ_GTFS" # Will need a way to automatically update this.
if gtfs_path is None or not os.path.exists(gtfs_path):
    raise ValueError("GTFS static data path is not set or does not exist! Please download the latest GTFS static data from TransLink and set the path correctly. https://translink.com.au/about-translink/open-data")
routes = pd.read_csv(os.path.join(gtfs_path, "routes.txt"))
trips = pd.read_csv(os.path.join(gtfs_path, "trips.txt"))
stops = pd.read_csv(os.path.join(gtfs_path, "stops.txt"))
stop_times = pd.read_csv(os.path.join(gtfs_path, "stop_times.txt"))

route_lookup = route_lookup = routes.set_index("route_id")[["route_short_name", "route_long_name", "route_color"]].to_dict("index")
trip_lookup = trips.set_index("trip_id")["trip_headsign"].to_dict()
stop_names = stops["stop_name"].tolist()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def resolve_stop_input(user_input: str):
    exact_id = stops[stops["stop_id"] == user_input]
    if not exact_id.empty:
        return exact_id.iloc[0]["stop_id"], exact_id.iloc[0]["stop_name"]

    exact_name = stops[stops["stop_name"].str.lower() == user_input.lower()]
    if not exact_name.empty:
        return exact_name.iloc[0]["stop_id"], exact_name.iloc[0]["stop_name"]

    fuzzy_matches = stops[stops["stop_name"].str.contains(user_input, case=False, na=False)]
    if not fuzzy_matches.empty:
        fuzzy_matches["length"] = fuzzy_matches["stop_name"].str.len()
        best_match = fuzzy_matches.sort_values("length").iloc[0]
        return best_match["stop_id"], best_match["stop_name"]

    return None, None

async def stop_autocomplete(interaction: discord.Interaction, current: str):
    user_id = str(interaction.user.id)
    filepath = os.path.join("users", f"{user_id}_pins.txt")
    pinned_names = []

    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            pinned_ids = [line.strip() for line in f if line.strip()]
            for pid in pinned_ids:
                match = stops[stops["stop_id"] == pid]
                if not match.empty:
                    pinned_names.append("⭐ " + match.iloc[0]["stop_name"])

    results = [name for name in stop_names if current.lower() in name.lower() and name not in [n.replace("⭐ ", "") for n in pinned_names]]
    combined = pinned_names + results
    combined = combined[:25]

    await interaction.response.autocomplete([
        app_commands.Choice(name=name, value=name.replace("⭐ ", ""))
        for name in combined
    ])

# RESYNC
@bot.event
async def on_ready():
    print(f"logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Game(name="with GTFS data! See me online? Come try me out!")
    )
    print("syncing commands globally.")
    await bot.tree.sync()
    print("global command sync complete!")

@bot.tree.command(name="ping", description="check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("👋 Hey! You've caught me online. Try the /view command. <:HeavyRail:1385977140448858225>", ephemeral=False)

# Pin System

@bot.tree.command(name="pin", description="Pin your favourite stops.")
@app_commands.describe(stop_id="Requires numerical stop ID")
@app_commands.autocomplete(stop_id=stop_autocomplete)
async def pin(interaction: discord.Interaction, stop_id: str):
    await interaction.response.defer(ephemeral=True)
    
    user_id = str(interaction.user.id)
    user_dir = "users"
    os.makedirs(user_dir, exist_ok=True)
    filepath = os.path.join(user_dir, f"{user_id}_pins.txt")
    
    try:
        with open(filepath, "a") as f:
            f.write(stop_id + "\n")
        await interaction.followup.send(f"📌 Stop `{stop_id}` pinned!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Failed to pin stop: `{e}`", ephemeral=True)

class ViewRefreshView(View):
    def __init__(self, stop_id, stop_real_name):
        super().__init__(timeout=60)
        self.stop_id = stop_id
        self.stop_real_name = stop_real_name

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary)
    async def refresh_button(self, interaction_button: discord.Interaction, button: discord.ui.Button):
        if interaction_button.response.is_done():
            await interaction_button.followup.send("❌ This interaction has expired. Please run /view again.", ephemeral=True)
            return
        else:
            await interaction_button.response.defer()

        url = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates"
        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    data = await resp.read()
                    feed.ParseFromString(data)
        except Exception as e:
            await interaction_button.followup.send(f"⚠️ error fetching live data:\n`{e}`", ephemeral=True)
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())
        upcoming = []

        for entity in feed.entity:
            if entity.HasField('trip_update'):
                trip = entity.trip_update
                trip_id = trip.trip.trip_id
                route_id = trip.trip.route_id

                for stu in trip.stop_time_update:
                    if stu.stop_id == self.stop_id:
                        arrival_ts = stu.arrival.time if stu.HasField("arrival") else stu.departure.time
                        if not arrival_ts or arrival_ts < now_ts:
                            continue

                        minutes = (arrival_ts - now_ts) // 60
                        route_info = route_lookup.get(route_id, {})
                        route_num = route_info.get("route_short_name", route_id)
                        route_name = route_info.get("route_long_name", "Unknown route")
                        route_color = route_info.get("route_color", "00FF00")
                        destination = trip_lookup.get(trip_id, "Unknown destination")

                        upcoming.append({
                            "route_num": route_num,
                            "route_name": route_name,
                            "route_color": route_color,
                            "destination": destination,
                            "minutes": minutes
                        })

        upcoming.sort(key=lambda x: x["minutes"])
        next_five = upcoming[:8]

        if not next_five:
            embed = discord.Embed(
                title=f"No upcoming departures found for {self.stop_real_name}",
                color=discord.Color.red()
            )
            embed.set_footer(text="💡 At this time, only Real Time information is being shown. Services lacking Real Time Transit will not be shown.")
            await interaction_button.edit_original_response(embed=embed, view=self)
            return

        embed = discord.Embed(
            title=f"Next departures at {self.stop_real_name}",
            color=discord.Color.blue()
        )
        header = f"ID   {'Route':<37}Time"
        rows = []
        for svc in next_five:
            ansi_code = closest_ansi_color(svc.get("route_color", "000000"))
            colored_dest = f"{svc['route_num']} \x1b[{ansi_code}m{svc['destination'][:34]:<35}\x1b[0m"
            time_str = f"{str(svc['minutes']) + ' min':>6}"
            rows.append(f"{colored_dest}{time_str}")
        embed.description = f"```ansi\n{header}\n" + "\n".join(rows) + "\n```"
        embed.set_footer(text="💡 For now, only Real Time information is being shown. Services with RTT off will not be shown.")
        await interaction_button.edit_original_response(embed=embed, view=self)

@bot.tree.command(name="offlineview", description="Show next 8 scheduled services for a Translink stop (no real-time)")
@app_commands.describe(stop_name="Stop name or stop ID")
@app_commands.autocomplete(stop_name=stop_autocomplete)
async def offlineview(interaction: discord.Interaction, stop_name: str):
    stop_id, stop_real_name = resolve_stop_input(stop_name)
    if not stop_id:
        await interaction.response.send_message(f"❌ couldn't find stop `{stop_name}`", ephemeral=True)
        return

    await interaction.response.defer()

    now = datetime.now()
    today_trips = stop_times[stop_times["stop_id"] == stop_id].copy()

    def parse_arrival(time_str):
        try:
            h, m, s = map(int, time_str.split(":"))
            base = now.replace(hour=0, minute=0, second=0, microsecond=0)
            delta = timedelta(hours=h, minutes=m, seconds=s)
            return base + delta
        except:
            return None

    today_trips["arrival_dt"] = today_trips["arrival_time"].apply(parse_arrival)
    print(f"✅ {len(today_trips)} trip entries after parsing")
    print(today_trips[["trip_id", "arrival_time", "arrival_dt"]].head(10))
    today_trips = today_trips[today_trips["arrival_dt"].notnull()]
    today_trips = today_trips[today_trips["arrival_dt"] >= now]
    merged = today_trips.merge(trips, on="trip_id")
    print(f"🔗 Merged trips: {len(merged)}")
    merged.sort_values("arrival_dt", inplace=True)

    upcoming = []
    for _, row in merged.head(8).iterrows():
        trip_id = row["trip_id"]
        route_id = row["route_id"]
        minutes = int((row["arrival_dt"] - now).total_seconds() // 60)
        route_info = route_lookup.get(route_id, {})
        route_num = route_info.get("route_short_name", route_id)
        destination = trip_lookup.get(trip_id, "Unknown destination")
        color = route_info.get("route_color", "000000")

        upcoming.append({
            "route_num": route_num,
            "destination": destination,
            "route_color": color,
            "minutes": minutes
        })

    embed = discord.Embed(
        title=f"Next scheduled departures at {stop_real_name}",
        color=discord.Color.teal()
    )

    header = f"{'Service':<37}Time"
    rows = []
    for svc in upcoming:
        ansi_code = closest_ansi_color(svc.get("route_color", "000000"))
        dest = f"\x1b[{ansi_code}m{svc['destination'][:34]:<35}\x1b[0m"
        rows.append(f"{svc['route_num']} {dest}{svc['minutes']:>6} min")

    embed.description = f"```ansi\n{header}\n" + "\n".join(rows) + "\n```"
    embed.set_footer(text="🕓 This list uses only scheduled (static) data.")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="view", description="Show next 5 services for a Translink stop")
@app_commands.describe(stop_name="Stop name or stop ID")
@app_commands.autocomplete(stop_name=stop_autocomplete)
async def view(interaction: discord.Interaction, stop_name: str):
    stop_id, stop_real_name = resolve_stop_input(stop_name)
    if not stop_id:
        await interaction.response.send_message(f"❌ couldn't find stop `{stop_name}`", ephemeral=True)
        return

    await interaction.response.defer()

    url = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates"
    # url = "https://gtfsrt.api.translink.com.au/api/realtime/CNS/TripUpdates"

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.read()
                feed.ParseFromString(data)
    except Exception as e:
        await interaction.followup.send(f"⚠️ error fetching live data:\n`{e}`")
        return

    now_ts = int(datetime.now(timezone.utc).timestamp())
    upcoming = []

    for entity in feed.entity:
        if entity.HasField('trip_update'):
            trip = entity.trip_update
            trip_id = trip.trip.trip_id
            route_id = trip.trip.route_id

            for stu in trip.stop_time_update:
                if stu.stop_id == stop_id:
                    arrival_ts = stu.arrival.time if stu.HasField("arrival") else stu.departure.time
                    if not arrival_ts or arrival_ts < now_ts:
                        continue

                    minutes = (arrival_ts - now_ts) // 60
                    route_info = route_lookup.get(route_id, {})
                    route_num = route_info.get("route_short_name", route_id)
                    route_name = route_info.get("route_long_name", "Unknown route")
                    route_color = route_info.get("route_color", "00FF00")  # default green if missing
                    destination = trip_lookup.get(trip_id, "Unknown destination")
                    print(route_name, route_color)
                    upcoming.append({
                        "route_num": route_num,
                        "route_name": route_name,
                        "route_color": route_color,
                        "destination": destination,
                        "minutes": minutes
                    })

    upcoming.sort(key=lambda x: x["minutes"])
    next_five = upcoming[:8]

    if not next_five:
        embed = discord.Embed(
            title=f"No upcoming departures found for {stop_real_name}",
            color=discord.Color.red()
        )
        embed.set_footer(text="💡 At this time, only Real Time information is being shown. Services lacking Real Time Transit will not be shown.")
        await interaction.followup.send(embed=embed, view=ViewRefreshView(stop_id, stop_real_name))
        return

    embed = discord.Embed(
        title=f"Next departures at {stop_real_name}",
        color=discord.Color.blue()
    )

    header = f"ID   {'Route':<37}Time"

    rows = []
    for svc in next_five:
        ansi_code = closest_ansi_color(svc.get("route_color", "000000"))
        print(f"Using ANSI code: {ansi_code}{svc.get('route_color', '000000')} for route {svc['route_num']}")
        colored_dest = f"{svc['route_num']} \x1b[{ansi_code}m{svc['destination'][:34]:<35}\x1b[0m"
        time_str = f"{str(svc['minutes']) + ' min':>6}"
        rows.append(f"{colored_dest}{time_str}")
    
    # Embed
    embed.description = f"```ansi\n{header}\n" + "\n".join(rows) + "\n```"
    embed.set_footer(text="💡 For now, only Real Time information is being shown. Services with RTT off will not be shown.")

    await interaction.followup.send(embed=embed, view=ViewRefreshView(stop_id, stop_real_name))

@bot.tree.command(name="timetable", description="View static timetable for a route at a specific time")
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

                        def parse_arrival(t):
                            try:
                                h, m, s = map(int, t.split(":"))
                                base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
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
                            services.append(f"{formatted_time} — \x1b[1;35m{stop_name}\x1b[0m")

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

        def parse_arrival(t):
            try:
                h, m, s = map(int, t.split(":"))
                base = now.replace(hour=0, minute=0, second=0, microsecond=0)
                delta = timedelta(hours=h, minutes=m, seconds=s)
                return base + delta
            except:
                return None

        stop_times_for_trips["arrival_dt"] = stop_times_for_trips["arrival_time"].apply(lambda t: parse_arrival(t))
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
    bot.run(TOKEN)