import os
import discord
import pandas as pd
import aiohttp
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from google.transit import gtfs_realtime_pb2

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

gtfs_path = "gtfs_static" # Will need a way to automatically update this.
if gtfs_path is None or not os.path.exists(gtfs_path):
    raise ValueError("GTFS static data path is not set or does not exist! Please download the latest GTFS static data from TransLink and set the path correctly. https://translink.com.au/about-translink/open-data")
routes = pd.read_csv(os.path.join(gtfs_path, "routes.txt"))
trips = pd.read_csv(os.path.join(gtfs_path, "trips.txt"))
stops = pd.read_csv(os.path.join(gtfs_path, "stops.txt"))

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
    results = [name for name in stop_names if current.lower() in name.lower()]
    return [
        app_commands.Choice(name=name, value=name)
        for name in results[:25]
    ]

# RESYNC
@bot.event
async def on_ready():
    print(f"logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Game(name="with GTFS data! See me online? Come try me out!")
    )
#     print("syncing commands globally, this can take up to an hour...")
#     await bot.tree.sync()
#     print("global command sync complete!")

@bot.tree.command(name="ping", description="check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("👋 Hey! You've caught me online. Try the /view command.", ephemeral=False)

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
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(
        title=f"Next departures at {stop_real_name}",
        color=discord.Color.blue()
    )

    header = f"ID   {'Route':<37}Time"

    rows = []
    for svc in next_five:
        ansi_code = closest_ansi_color(svc.get("route_color", "000000"))
        print(f"Using ANSI code: {ansi_code}{svc.get("route_color", "000000")} for route {svc['route_num']}")
        colored_dest = f"{svc['route_num']} \x1b[{ansi_code}m{svc['destination'][:34]:<35}\x1b[0m"
        time_str = f"{str(svc['minutes']) + ' min':>6}"
        rows.append(f"{colored_dest}{time_str}")
    
    # Embed
    embed.description = f"```ansi\n{header}\n" + "\n".join(rows) + "\n```"
    embed.set_footer(text="💡 For now, only Real Time information is being shown. Services with RTT off will not be shown.")


    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    bot.run(TOKEN)