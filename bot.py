def get_current_service_period(now_local=None):
    """Return the current service period label with the appropriate custom emojis."""
    if now_local is None:
        now_local = datetime.now().astimezone()
    wd = now_local.weekday()  # Monday=0 ... Sunday=6
    minutes = now_local.hour * 60 + now_local.minute

    overnight = "<:ZSEQTA_ON_1:1439159264290799736><:ZSEQTA_ON_2:1439159305990439005><:ZSEQTA_ON_3:1439159360323588218> Overnight"
    peak = "<:ZSEQTA_PP_1:1439091104145211433><:ZSEQTA_PP_2:1439091154950819950><:ZSEQTA_PP_3:1439091192745689088><:ZSEQTA_PP_4:1439091229420687512> Peak Period"
    offpeak_weekday = "<:ZSEQTA_OP_1:1439160067181248523><:ZSEQTA_OP_2:1439160102505414770><:ZSEQTA_OP_3:1439160143681032203> Off-Peak Weekday"
    offpeak_evening = "<:ZSEQTA_OP_1:1439160067181248523><:ZSEQTA_OP_2:1439160102505414770><:ZSEQTA_OP_3:1439160143681032203> Off-Peak Evening"
    nightlink = "<:ZSEQTA_NL_1:1439053082863472733><:ZSEQTA_NL_2:1439053039112556584><:ZSEQTA_NL_3:1439052995005382937><:ZSEQTA_NL_4:1439052948981415986> NightLink Service Hours"
    weekend = "<:ZSEQTA_WN_1:1439162176979075132><:ZSEQTA_WN_2:1439162209744715818><:ZSEQTA_WN_3:1439162248403619883> Weekend"

    # NightLink: Friday/Saturday nights 11:51pm–6:00am the following day
    if ((wd in (4, 5) and minutes >= 23 * 60 + 51) or (wd in (5, 6) and minutes <= 6 * 60)):
        return nightlink

    # Weekend (Saturday/Sunday) daytime band 6:01am–6:01pm
    if wd in (5, 6):
        if 6 * 60 + 1 <= minutes <= 18 * 60 + 1:
            return weekend

        if wd == 6:
            # Sunday evening special rules
            if 18 * 60 + 1 <= minutes <= 22 * 60:
                return offpeak_evening
            if 22 * 60 + 1 <= minutes <= 23 * 60 + 59:
                return overnight
        else:
            # Saturday evening (outside NightLink)
            if 18 * 60 + 1 <= minutes <= 23 * 60 + 50:
                return offpeak_evening
        # Fallback on weekends
        return overnight

    # Weekdays (Monday–Friday, with Friday late night handled above)
    if 0 <= minutes <= 5 * 60 + 50:
        return overnight
    if 5 * 60 + 51 <= minutes <= 9 * 60:
        return peak
    if 9 * 60 + 1 <= minutes <= 14 * 60 + 50:
        return offpeak_weekday
    if 14 * 60 + 51 <= minutes <= 18 * 60:
        return peak
    if 18 * 60 + 1 <= minutes <= 22 * 60:
        return offpeak_evening
    if 22 * 60 + 1 <= minutes <= 23 * 60 + 59:
        return overnight

    return overnight
import os
import sys
import discord
import pandas as pd
import aiohttp
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

from google.transit import gtfs_realtime_pb2
import re

# Helper to split long strings into chunks <= max_len, preferring to break at newlines.
def split_message(text: str, max_len: int = 1990):
    """Split a long string into chunks <= max_len, preferring to break at newlines."""
    chunks = []
    remaining = text
    while len(remaining) > max_len:
        # try to cut at the last newline before max_len
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            # no good newline; hard split
            split_at = max_len
        chunk = remaining[:split_at]
        chunks.append(chunk)
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    if remaining:
        chunks.append(remaining)
    return chunks

# optional runtime configuration for network status posting
# ensure environment variables are loaded before reading them
load_dotenv()
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID")) if os.getenv("STATUS_CHANNEL_ID") else None
GTFS_RT_URL = os.getenv("GTFS_RT_URL")  # e.g. Translink SEQ GTFS-RT alerts URL (set this to the Translink Alerts feed)
STATUS_UPDATE_INTERVAL = int(os.getenv("STATUS_UPDATE_INTERVAL", "300"))  # seconds between updates
STATUS_SETTINGS_CHANNEL_ID = int(os.getenv("STATUS_SETTINGS_CHANNEL_ID")) if os.getenv("STATUS_SETTINGS_CHANNEL_ID") else 1439115773623799910
# Main Translink Status message (services + major notices)
STATUS_MESSAGE_ID = int(os.getenv("STATUS_MESSAGE_ID", "1439171270209048636"))
# Extra Statistics message (second message under the status)
STATUS_STATS_MESSAGE_ID = int(os.getenv("STATUS_STATS_MESSAGE_ID", "1439171272251539518"))

async def fetch_gtfs_rt_alerts(session: aiohttp.ClientSession):
    """Fetch GTFS-RT feed from GTFS_RT_URL and return a list of Alert objects.
    If the fetch or parse fails, return an empty list (caller may fall back to track module).
    """
    alerts = []
    if not GTFS_RT_URL:
        return alerts
    try:
        async with session.get(GTFS_RT_URL, timeout=20) as resp:
            if resp.status != 200:
                print(f"GTFS-RT HTTP {resp.status}")
                return alerts
            data = await resp.read()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(data)
            for entity in feed.entity:
                if entity.HasField("alert"):
                    alerts.append(entity.alert)
    except Exception as e:
        print("GTFS-RT fetch/parse error:", e)
    return alerts


def route_id_to_shortname(route_id: str):
    info = route_lookup.get(route_id)
    if not info:
        return route_id
    return info.get("route_short_name") or info.get("route_long_name") or route_id



# Helper to normalise terminus names so GTFS and config keys line up
def normalize_terminus_name(name: str) -> str:
    """Normalise a terminus name so GTFS long_names and config names line up."""
    if not isinstance(name, str):
        return ""
    s = name.strip()
    # strip common suffixes like 'line' and 'station'
    s = re.sub(r'\bline\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\bstation\b', '', s, flags=re.IGNORECASE)
    # collapse multiple spaces and lower-case
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s

# Mapping from line keywords (as they appear in Translink notices) to canonical rail termini.
# Used to stop closures for one line (e.g. Gold Coast) from incorrectly applying to others
# (e.g. Airport, Beenleigh, Doomben) that share inner-city tracks or route_ids.
# Mapping from line keywords (as they appear in Translink notices) to canonical rail termini.
# Used to stop closures for one line (e.g. Gold Coast) from incorrectly applying to others
# (e.g. Airport, Beenleigh, Doomben) that share inner-city tracks or route_ids.
LINE_KEYWORD_TO_TERMINI = {
    "gold coast line": ["Varsity Lakes"],
    "gold coast lines": ["Varsity Lakes"],
    "shorncliffe line": ["Shorncliffe"],
    "shorncliffe lines": ["Shorncliffe"],
    "beenleigh line": ["Beenleigh"],
    "cleveland line": ["Cleveland"],
    "caboolture line": ["Caboolture"],
    "redcliffe peninsula line": ["Redcliffe"],
    "airport line": ["Airport"],
    "doomben line": ["Doomben"],
    "fern grove line": ["Ferny Grove"],
    "ferny grove line": ["Ferny Grove"],
    "ipswich line": ["Ipswich"],
    "rosewood line": ["Rosewood"],
    "sunshine coast line": ["Nambour", "Gympie North"],
    "nambour line": ["Nambour", "Gympie North"],
}

# Optional display-name overrides for termini in the Services list
TERMINUS_DISPLAY_NAME = {
    # example: show line colour / emoji for Rosewood
    "Rosewood": "<:CAIP:1439168898149650493> Rosewood",
    "Ipswich": "<:CAIP:1439168898149650493> Ipswich",
    "Nambour": "<:CAIP:1439168898149650493> Nambour",
    # shorten long names
    "Redcliffe Peninsula": "<:SCRP:1439169569116782655> Redcliffe",
    "Shorncliffe": "<:SHCL:1439169376287850576> Shorncliffe",
    "Cleveland": "<:SHCL:1439169376287850576> Cleveland",
    "Airport": "<:VLBD:1439168959323701300> Brisbane Airport",
    "Varsity Lakes": "<:VLBD:1439168959323701300> Gold Coast",
    "Doomben": "<:Doombarino:1439169746170810508> Doomben",
    "Caboolture": "<:CAIP:1439168898149650493> Caboolture",
    "Ferny Grove": "<:FGBN:1439169373872062565> Ferny Grove",
    "Beenleigh": "<:FGBN:1439169373872062565> Beenleigh",
    "Springfield": "<:SCRP:1439169569116782655> Springfield Central",
    "Gympie North": "<:CAIP:1439168898149650493> Gympie",
    "Brisbane City": "<:BLNK:1439169855310790668> Brisbane City",
    # you can add more here later if desired
    # e.g. "Gold Coast": "💛 Gold Coast",
}

# --- Status Settings Helpers ---
async def fetch_status_settings():
    """Fetch status settings from a dedicated Discord message.

    Looks for a message starting with '# Status Settings' in STATUS_SETTINGS_CHANNEL_ID
    and parses key: value lines such as:
        debugMode: true
        manuallyOverrideNotices: true
        noticeOverride: "`...`"
    """
    # default settings
    env_debug = os.getenv("STATUS_DEBUG_MODE")
    settings = {
        # allow an env var override; the Discord settings message can still change this
        "debugMode": env_debug.lower() in ("true", "1", "yes", "on") if env_debug is not None else False,
        "manuallyOverrideNotices": False,
        "noticeOverride": None,
    }

    if not STATUS_SETTINGS_CHANNEL_ID:
        return settings

    try:
        channel = bot.get_channel(STATUS_SETTINGS_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(STATUS_SETTINGS_CHANNEL_ID)
    except Exception as e:
        print("status settings: failed to access settings channel:", e)
        return settings

    def parse_settings_from_text(text: str, base: dict) -> dict:
        # Strip Discord-style code fences if present (``` or ```md etc)
        stripped = text.strip()
        if stripped.startswith("```") and stripped.endswith("```") and len(stripped) >= 6:
            # remove leading and trailing ```
            stripped = stripped[3:-3].strip()
        else:
            stripped = text

        lines = stripped.splitlines()
        if not lines:
            return base

        # Find the line that contains the '# Status Settings' header (case-insensitive)
        header_index = 0
        for idx, raw in enumerate(lines):
            if raw.strip().lower().startswith("# status settings"):
                header_index = idx
                break

        # Parse lines after the header
        for raw in lines[header_index + 1:]:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()

            # strip surrounding backticks or quotes if present
            if value.startswith("`") and value.endswith("`") and len(value) >= 2:
                value = value[1:-1].strip()
            if ((value.startswith('"') and value.endswith('"')) or
                (value.startswith("'") and value.endswith("'"))) and len(value) >= 2:
                value = value[1:-1].strip()

            v_lower = value.lower()

            if key == "debugMode":
                base["debugMode"] = v_lower in ("true", "1", "yes", "on")
            elif key == "manuallyOverrideNotices":
                base["manuallyOverrideNotices"] = v_lower in ("true", "1", "yes", "on")
            elif key == "noticeOverride":
                # keep raw value (which may contain emojis / markdown)
                base["noticeOverride"] = value
            else:
                # unknown keys are ignored for now
                pass
        return base

    try:
        async for msg in channel.history(limit=50):
            if isinstance(msg.content, str) and "# status settings" in msg.content.lower():
                settings = parse_settings_from_text(msg.content, settings)
                break
    except Exception as e:
        print("status settings: error reading history:", e)

    return settings


def classify_alert_for_route(alert, route_id: str):
    """Classify a GTFS-RT Alert for a given route.

    Returns one of:
        'closure'  - current track/rail closure (no trains / bus replacement etc)
        'upcoming' - future incident/closure
        'active'   - active non-closure incident
        'unknown'  - everything else
    """
    now_local = datetime.now().astimezone()
    today = now_local.date()
    one_week_ahead = today + timedelta(days=7)
    active = False
    upcoming = False

    # --- Step 1: Prefer GTFS-RT active_period for timing ---
    has_period = False
    try:
        for ap in alert.active_period:
            has_period = True
            start_dt = None
            end_dt = None
            if ap.start:
                start_dt = datetime.fromtimestamp(ap.start, timezone.utc).astimezone()
            if ap.end:
                end_dt = datetime.fromtimestamp(ap.end, timezone.utc).astimezone()

            # Skip periods that are entirely in the past
            if end_dt and end_dt < now_local:
                continue

            # Current if now is between start and end (or after start when no end given)
            if start_dt and start_dt <= now_local and (end_dt is None or now_local <= end_dt):
                active = True
                break

            # Upcoming if it starts within the next 7 days
            if start_dt and now_local < start_dt <= now_local + timedelta(days=7):
                upcoming = True
    except Exception:
        has_period = has_period or False

    # --- Step 2: Fallback to parsing dates from the human-readable text if no active_period ---
    # This covers the edge case where Translink omits active_period but includes dates in the text.
    if not has_period:
        header_text = ""
        desc_text = ""
        try:
            if alert.header_text and getattr(alert.header_text, "translation", None):
                if len(alert.header_text.translation) > 0 and alert.header_text.translation[0].text:
                    header_text = alert.header_text.translation[0].text
        except Exception:
            header_text = ""
        try:
            if alert.description_text and getattr(alert.description_text, "translation", None):
                if len(alert.description_text.translation) > 0 and alert.description_text.translation[0].text:
                    desc_text = alert.description_text.translation[0].text
        except Exception:
            desc_text = ""

        full_text = (header_text + " " + desc_text).strip()
        explicit_date = None
        try:
            # Try to find any "<day> <Month> [year]" pattern in the text.
            m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?", full_text, re.IGNORECASE)
            if m:
                day = int(m.group(1))
                month_name = m.group(2)
                year_str = m.group(3)
                year = today.year if not year_str else int(year_str)

                parsed = None
                try:
                    parsed = datetime.strptime(f"{day} {month_name} {year}", "%d %B %Y").date()
                except ValueError:
                    try:
                        parsed = datetime.strptime(f"{day} {month_name} {year}", "%d %b %Y").date()
                    except ValueError:
                        parsed = None

                if parsed is not None:
                    if parsed < today - timedelta(days=180):
                        parsed = parsed.replace(year=parsed.year + 1)
                    explicit_date = parsed
        except Exception:
            explicit_date = None

        if explicit_date is not None:
            if explicit_date <= today:
                active = True
            elif today < explicit_date <= one_week_ahead:
                upcoming = True

    # --- Step 3: Determine whether this is a closure vs a generic incident ---

    header_text = ""
    desc_text = ""
    try:
        if alert.header_text and getattr(alert.header_text, "translation", None):
            if len(alert.header_text.translation) > 0 and alert.header_text.translation[0].text:
                header_text = alert.header_text.translation[0].text
    except Exception:
        header_text = ""
    try:
        if alert.description_text and getattr(alert.description_text, "translation", None):
            if len(alert.description_text.translation) > 0 and alert.description_text.translation[0].text:
                desc_text = alert.description_text.translation[0].text
    except Exception:
        desc_text = ""

    full_text = (header_text + " " + desc_text).strip()
    text_lower = full_text.lower()

    closure_keywords = [
        "track closure",
        "track closures",
        "rail closure",
        "no trains",
        "no train services",
        "bus replacement",
    ]
    has_closure_words = any(k in text_lower for k in closure_keywords)

    effect_value = getattr(alert, "effect", None)
    closure_effects = set()
    try:
        closure_effects = {
            gtfs_realtime_pb2.Alert.NO_SERVICE,
            gtfs_realtime_pb2.Alert.REDUCED_SERVICE,
            gtfs_realtime_pb2.Alert.DETOUR,
            gtfs_realtime_pb2.Alert.MODIFIED_SERVICE,
        }
    except Exception:
        pass

    is_closure_effect = effect_value in closure_effects

    if active:
        if is_closure_effect or has_closure_words:
            return "closure"
        return "active"

    if upcoming:
        return "upcoming"

    return "unknown"


async def build_status_message(limit_routes: int = 40, settings: dict | None = None):
    """Builds the status message string using GTFS-RT alerts.
    Identifies rail termini and explicitly lists current closures with their alert headers.
    """
    if settings is None:
        settings = {}
    async with aiohttp.ClientSession() as session:
        alerts = await fetch_gtfs_rt_alerts(session)

    # use track.py helpers for time and alert stats
    now_local = datetime.now().astimezone()
    try:
        alerts_today = track.count_alerts_today(alerts, now_local)
    except Exception as e:
        print("warning computing alerts_today via track:", e)
        alerts_today = len(alerts)

    # map route_id -> list of alerts
    # we only use alerts that explicitly reference a route_id to avoid
    # over-propagating closures via shared stops (e.g. Central, Roma Street)
    route_alerts = {}
    for alert in alerts:
        try:
            for ie in alert.informed_entity:
                rid = getattr(ie, 'route_id', None)
                if not rid:
                    continue
                rid_str = str(rid).strip()
                if not rid_str:
                    continue
                route_alerts.setdefault(rid_str, []).append(alert)
        except Exception:
            continue

    # Build termini -> set(route_id) mapping for rail routes (route_type == 2)
    termini_map: dict[str, set] = {}
    try:
        rail_routes = routes[routes.get('route_type', '').astype(str) == '2']
    except Exception:
        rail_routes = routes.copy()

    for _, r in rail_routes.iterrows():
        route_id = r['route_id']
        long_name = str(r.get('route_long_name') or r.get('route_short_name') or '')
        parts = []
        if ' - ' in long_name:
            parts = [p.strip() for p in long_name.split(' - ') if p.strip()]
        elif ' to ' in long_name.lower():
            parts = [p.strip() for p in re.split(r'\bto\b', long_name, flags=re.IGNORECASE) if p.strip()]
        elif '/' in long_name:
            parts = [p.strip() for p in long_name.split('/') if p.strip()]
        else:
            if ' via ' in long_name.lower():
                parts = [p.strip() for p in re.split(r'\bvia\b', long_name, flags=re.IGNORECASE) if p.strip()]
            else:
                parts = [long_name.strip()]

        if len(parts) >= 2:
            endpoints = [parts[0], parts[-1]]
        else:
            endpoints = [parts[0]]

        for ep in endpoints:
            if not ep:
                continue
            termini_map.setdefault(ep, set()).add(route_id)

    # Explicitly add non-rail services that we still want to appear in the Services list
    # Metro 1 (M1), Metro 2 (M2), and Gold Coast Light Rail (GCR1)
    try:
        extra_services = {
            "M1": "<:M1:1439171140353261660> Metro 1",
            "M2": "<:M2:1439171138575011961> Metro 2",
            "GCR1": "<:GCLR:1439172376481894401> Gold Coast Light Rail",
        }
        for short_name, label in extra_services.items():
            matches = routes[routes["route_short_name"].astype(str).str.strip().str.upper() == short_name]
            for _, er in matches.iterrows():
                rid = er["route_id"]
                termini_map.setdefault(label, set()).add(rid)
    except Exception as e:
        print("warning adding extra services to termini map:", e)


    # Determine status for each terminus and collect closure reasons
    # We also categorise lines so we can list current closures first in the Termini section.
    termini_closure_lines = []   # "**X**: cross Current closure"
    termini_upcoming_lines = []  # "**X**: ⚠️ Upcoming Incident"
    termini_operational_lines = []  # "**X**: check Operational"
    closures_map = {}  # terminus -> list of alert headers that caused current closure
    debug_notice_links = {}  # terminus -> list of (kind, title, url) for debug output

    for terminus in sorted(termini_map.keys(), key=lambda s: s.lower()):
        associated_routes = termini_map[terminus]
        status = "Operational"
        emoji = "<:SEQTA_TICK:1439134843953877012>"
        closure_reasons = []

        for rid in associated_routes:
            alerts_for_route = route_alerts.get(rid, [])
            for a in alerts_for_route:
                cls = classify_alert_for_route(a, rid)
                # prefer active closures
                if cls == 'closure':
                    # --- extra guard: only apply this closure to a terminus if the notice text
                    # actually targets that line or that terminus, to avoid e.g. Gold Coast
                    # closures marking Airport / Beenleigh / Doomben as closed.
                    header_txt = ""
                    desc_txt = ""
                    try:
                        if a.header_text and getattr(a.header_text, "translation", None):
                            if len(a.header_text.translation) > 0 and a.header_text.translation[0].text:
                                header_txt = a.header_text.translation[0].text
                    except Exception:
                        pass
                    try:
                        if a.description_text and getattr(a.description_text, "translation", None):
                            if len(a.description_text.translation) > 0 and a.description_text.translation[0].text:
                                desc_txt = a.description_text.translation[0].text
                    except Exception:
                        pass

                    full_txt_lower = (header_txt + " " + desc_txt).lower()

                    # 1) direct station/terminus mention
                    term_lower = terminus.lower()
                    matches_terminus_name = term_lower in full_txt_lower

                    # 2) explicit line-name mapping (e.g. "Gold Coast line" -> Varsity Lakes)
                    matches_line_keyword = False
                    for kw, termini_list in LINE_KEYWORD_TO_TERMINI.items():
                        if kw in full_txt_lower and terminus in termini_list:
                            matches_line_keyword = True
                            break

                    # 3) special network-wide wording ("all lines" etc) still applies everywhere
                    network_wide = (
                        "all lines" in full_txt_lower
                        or "all train lines" in full_txt_lower
                        or "entire network" in full_txt_lower
                    )

                    if not (matches_terminus_name or matches_line_keyword or network_wide):
                        # this closure notice is not really about this terminus; skip it
                        continue

                    status = "Current issues"
                    emoji = "<:SEQTA_CROSS:1439135635905445978>"

                    header = header_txt or None
                    desc = desc_txt or None

                    reason_text = header or desc or f"Closure affecting route {rid}"
                    closure_reasons.append(reason_text)

                    # capture URL for debug, if provided in the alert
                    url_value = None
                    try:
                        if a.url and a.url.translation:
                            url_value = a.url.translation[0].text
                    except Exception:
                        url_value = None

                    debug_notice_links.setdefault(terminus, []).append(
                        ("closure", reason_text, url_value)
                    )

                    # once a current closure is found for this terminus, we can stop checking its routes
                    break
                if cls == 'upcoming' and status != "Current closure":
                    status = "Upcoming closure"
                    emoji = "<:SEQTA_WARN:1439136797425668096>"

                    # capture basic info and URL for debug
                    header = None
                    desc = None
                    try:
                        if a.header_text and getattr(a.header_text, "translation", None):
                            if len(a.header_text.translation) > 0 and a.header_text.translation[0].text:
                                header = a.header_text.translation[0].text
                    except Exception:
                        header = None
                    try:
                        if a.description_text and getattr(a.description_text, "translation", None):
                            if len(a.description_text.translation) > 0 and a.description_text.translation[0].text:
                                desc = a.description_text.translation[0].text
                    except Exception:
                        desc = desc or None

                    reason_text = header or desc or f"Incident affecting route {rid}"

                    url_value = None
                    try:
                        if a.url and a.url.translation:
                            url_value = a.url.translation[0].text
                    except Exception:
                        url_value = None

                    debug_notice_links.setdefault(terminus, []).append(
                        ("upcoming", reason_text, url_value)
                    )
            if status == "Current closure":
                break

        if closure_reasons and status == "Current closure":
            # dedupe reasons for the Current Closures section
            deduped = list(dict.fromkeys(closure_reasons))
            closures_map[terminus] = deduped

        # Apply optional display-name overrides (e.g. emojis, shorter names)
        display_name = TERMINUS_DISPLAY_NAME.get(terminus, terminus)

        line = f"**{display_name}**: {emoji} {status}"
        if status == "Current closure":
            termini_closure_lines.append(line)
        elif status == "Upcoming Incident":
            termini_upcoming_lines.append(line)
        else:
            termini_operational_lines.append(line)

    # Extra statistics via track.py: routes today and RTT vehicle counts
    routes_today = 0
    rtt_counts = {"trains": 0, "buses": 0, "trams": 0}
    try:
        routes_today = track.get_routes_operating_today(now_local.date())
    except Exception as e:
        print("warning computing routes_today via track:", e)
    try:
        rtt_counts = await track.get_rtt_vehicle_counts()
    except Exception as e:
        print("warning computing RTT vehicle counts via track:", e)
        rtt_counts = {"trains": 0, "buses": 0, "trams": 0}

    # Combine in desired order: current closures first, then upcoming, then operational
    terminus_lines = termini_closure_lines + termini_upcoming_lines + termini_operational_lines

    # Build Major Notices similar to before
    major_notices = []
    for alert in alerts:
        try:
            header = None
            if alert.header_text and getattr(alert.header_text, "translation", None):
                if len(alert.header_text.translation) > 0 and alert.header_text.translation[0].text:
                    header = alert.header_text.translation[0].text
            if header:
                lowered = header.lower()
                if any(k in lowered for k in ['rail', 'line', 'track', 'closure', 'suspended', 'no service', 'bus replacement']):
                    major_notices.append(header)
                else:
                    for ie in alert.informed_entity:
                        rid = getattr(ie, 'route_id', None)
                        if rid and rid in rail_routes['route_id'].values:
                            major_notices.append(header)
                            break
        except Exception:
            continue

    major_notices = list(dict.fromkeys(major_notices))[:3]
    major_line = ''
    if major_notices:
        placeholders = ', '.join([n for n in major_notices])
        major_line = f"<:SEQTA_MJ_1:1431240294640386230><:SEQTA_MJ_2:1431240291565699113> **{placeholders}**"

    # If settings specify a manual override for notices, use it instead of auto-generated text
    if settings.get("manuallyOverrideNotices") and settings.get("noticeOverride"):
        override = str(settings["noticeOverride"]).strip()
        # if the whole string is wrapped in backticks, strip them so it renders normally
        if override.startswith("`") and override.endswith("`") and len(override) >= 2:
            override = override[1:-1].strip()
        major_line = override

    # Assemble message: bundle all termini into a single Services section, ordered so that
    # current closures appear first, then upcoming incidents, then operational lines.
    header = "# Translink Status:\n"

    services_section = "## Services:\n" + "\n".join(terminus_lines)

    current_period = get_current_service_period(now_local)

    extra_lines = [
        f"**Current service period**: {current_period}",
        f"**How many routes are operating today?**: {routes_today}",
        f"**Total trains driving**: {rtt_counts.get('trains', 0)}",
        f"**Total buses driving**: {rtt_counts.get('buses', 0)}",
        f"**Total trams driving**: {rtt_counts.get('trams', 0)}",
        f"**Total alerts in feed**: {len(alerts)}",
    ]
    extra_section = "## Extra Statistics:\n" + "\n".join(extra_lines)

    major_section = "## Major Notices:\n" + (major_line or "No major notices.")

    # main status message: header + services + major notices only
    msg = header + services_section + "\n" + major_section

    # second message: extra statistics only
    stats_msg = extra_section

    # safety: ensure under Discord length (1990) for the main status message
    MAX_LEN = 1990
    if len(msg) > MAX_LEN:
        # try keeping all current closures, then as many other lines as will fit
        non_closure_lines = [l for l in terminus_lines if "Current closure" not in l]
        closure_lines = [l for l in terminus_lines if "Current closure" in l]
        trimmed = closure_lines + non_closure_lines[:max(0, 10)]
        msg = header + "## Services:\n" + "\n".join(trimmed) + "\n" + major_section

    # Optional debug block if enabled via settings.
    # Instead of appending to the main message, we return this as a separate debug message.
    debug_msg = None
    if settings.get("debugMode"):
        debug_bits = [
            f"alerts: {len(alerts)}",
            f"termini: {len(termini_map)}",
            f"current closures: {len(closures_map)}",
            f"length: {len(msg)}"
        ]
        debug_section = "## Status Debug:\n" + ", ".join(debug_bits)

        # Add per-terminus notice sources with links where available.
        lines = []
        for term, entries in debug_notice_links.items():
            if not entries:
                continue
            # Prefer a closure-causing notice if one exists; otherwise fall back to the first entry.
            chosen = None
            for kind, title, url_value in entries:
                if kind == "closure":
                    chosen = (kind, title, url_value)
                    break
            if chosen is None:
                chosen = entries[0]

            kind, title, url_value = chosen
            label = "closure" if kind == "closure" else "upcoming"
            if url_value:
                # Discord supports markdown links: [text](url)
                lines.append(f"- {term} ({label}): [{title}]({url_value})")
            else:
                lines.append(f"- {term} ({label}): {title}")
        if lines:
            debug_section += "\n\n### Notice sources:\n" + "\n".join(lines[:20])

        # We deliberately do NOT truncate here; splitting happens when sending.
        debug_msg = debug_section

    return msg, stats_msg, debug_msg


async def network_status_loop():
    """Background task: post or update a single message in STATUS_CHANNEL_ID every STATUS_UPDATE_INTERVAL seconds.
    Improved debugging and reliability:
    - uses fetch_channel() when get_channel() returns None
    - robust logging of failures
    - falls back to sending a diagnostic DM to the bot owner if posting repeatedly fails
    """
    await bot.wait_until_ready()

    if not STATUS_CHANNEL_ID:
        print("STATUS_CHANNEL_ID not configured; skipping network status loop.")
        return

    # try to get channel from cache, otherwise fetch via API
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(STATUS_CHANNEL_ID)
        except Exception as e:
            print("Could not fetch status channel:", e)
            return

    print(f"network_status_loop started for channel: {STATUS_CHANNEL_ID} ({getattr(channel, 'name', 'unknown')})")

    last_message = None
    last_stats_message = None
    last_debug_message = None
    failed_edits = 0

    # First, try to bind to the specific status and stats message IDs provided
    if STATUS_MESSAGE_ID:
        try:
            last_message = await channel.fetch_message(STATUS_MESSAGE_ID)
            print(f"bound status message to existing message id {STATUS_MESSAGE_ID}")
        except Exception as e:
            print(f"could not fetch status message by id {STATUS_MESSAGE_ID}:", e)

    if STATUS_STATS_MESSAGE_ID:
        try:
            last_stats_message = await channel.fetch_message(STATUS_STATS_MESSAGE_ID)
            print(f"bound stats message to existing message id {STATUS_STATS_MESSAGE_ID}")
        except Exception as e:
            print(f"could not fetch stats message by id {STATUS_STATS_MESSAGE_ID}:", e)

    # If that failed, fall back to finding existing bot messages by content prefix
    if last_message is None or last_stats_message is None or last_debug_message is None:
        try:
            async for m in channel.history(limit=200):
                if m.author != bot.user or not isinstance(m.content, str):
                    continue
                if last_message is None and m.content.startswith("# Translink Status:"):
                    last_message = m
                elif last_stats_message is None and m.content.startswith("## Extra Statistics:"):
                    last_stats_message = m
                elif last_debug_message is None and m.content.startswith("## Status Debug:"):
                    last_debug_message = m
                if last_message and last_stats_message and last_debug_message:
                    break
        except Exception as e:
            print("error reading channel history:", e)

    while not bot.is_closed():
        try:
            settings = await fetch_status_settings()
            content, stats_content, debug_content = await build_status_message(settings=settings)
            if not content:
                print("build_status_message returned empty content; skipping send/edit")
            else:
                # main status message
                if last_message:
                    try:
                        await last_message.edit(content=content)
                        print("edited existing status message")
                        failed_edits = 0
                    except Exception as e:
                        failed_edits += 1
                        print(f"failed to edit status message (attempt {failed_edits}):", e)
                        try:
                            last_message = await channel.send(content)
                            print("sent new status message after edit failure")
                            failed_edits = 0
                        except Exception as send_err:
                            print("failed to send new status message after edit failure:", send_err)
                else:
                    try:
                        last_message = await channel.send(content)
                        print("sent initial status message")
                    except Exception as e:
                        failed_edits += 1
                        print(f"failed to send initial status message (attempt {failed_edits}):", e)

                # stats message (Extra Statistics) as a separate message
                if stats_content:
                    try:
                        if last_stats_message:
                            await last_stats_message.edit(content=stats_content)
                            print("edited existing stats message")
                        else:
                            last_stats_message = await channel.send(stats_content)
                            print("sent new stats message")
                    except Exception as e:
                        print("failed to send/edit stats message:", e)

                # optional debug/notice-sources message
                if debug_content:
                    try:
                        # Split into safe chunks if needed
                        chunks = split_message(debug_content, max_len=1990)
                        if not chunks:
                            pass
                        else:
                            # First chunk edits or creates the primary debug message
                            first_chunk = chunks[0]
                            if last_debug_message:
                                await last_debug_message.edit(content=first_chunk)
                                print("edited existing debug status message")
                            else:
                                last_debug_message = await channel.send(first_chunk)
                                print("sent new debug status message")

                            # Remaining chunks are sent as additional messages
                            for idx, extra_chunk in enumerate(chunks[1:], start=2):
                                await channel.send(extra_chunk)
                                print(f"sent extra debug status chunk {idx}")
                    except Exception as e:
                        print("failed to send/edit debug status message:", e)

            # If we've failed repeatedly, produce more diagnostics to logs
            if failed_edits >= 3:
                print("multiple failures sending/editing status message. Check bot permissions, channel visibility, and that the bot is in the target guild.")
                # reset counter so we don't spam logs every loop
                failed_edits = 0

        except Exception as e:
            print("network status loop error:", e)
            if "807" in str(e):
                print("network status loop: detected error containing '807'; attempting GTFS refresh and restart...")
                try:
                    track.update_gtfs_now_blocking("error 807 auto-recovery")
                except Exception as upd_err:
                    print("network status loop: GTFS auto-update during 807 recovery failed:", upd_err)
                try:
                    print("network status loop: restarting process after 807...")
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except Exception as rex:
                    print("network status loop: failed to exec self after 807:", rex)
        await asyncio.sleep(STATUS_UPDATE_INTERVAL)
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

# Static GTFS auto-download configuration (mirrors track.py settings)
GTFS_STATIC_URL = os.getenv("GTFS_STATIC_URL", "")
DOWNLOAD_ON_BOOT_FLAG = os.getenv("DownloadOnBoot", "").strip().lower() in ("true", "1", "yes", "on")

def ensure_gtfs_static():
    """Ensure SEQ_GTFS exists.

    If the directory is missing and DownloadOnBoot is enabled (and GTFS_STATIC_URL is set),
    download the GTFS zip synchronously and extract it before any GTFS CSVs are loaded.
    """
    gtfs_dir = "SEQ_GTFS"
    if os.path.exists(gtfs_dir):
        return

    if not DOWNLOAD_ON_BOOT_FLAG or not GTFS_STATIC_URL:
        print("ensure_gtfs_static: SEQ_GTFS missing and either DownloadOnBoot is disabled or GTFS_STATIC_URL is not set.")
        return

    print("ensure_gtfs_static: SEQ_GTFS missing; downloading GTFS static before initialisation...")
    try:
        import zipfile
        from io import BytesIO
        import urllib.request
        import shutil

        with urllib.request.urlopen(GTFS_STATIC_URL, timeout=120) as resp:
            data = resp.read()

        tmp_dir = gtfs_dir + "_new_boot"
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)

        with zipfile.ZipFile(BytesIO(data)) as zf:
            zf.extractall(tmp_dir)

        backup_dir = gtfs_dir + "_old_boot"
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        if os.path.exists(gtfs_dir):
            os.rename(gtfs_dir, backup_dir)
        os.rename(tmp_dir, gtfs_dir)

        print("ensure_gtfs_static: GTFS static downloaded and extracted into SEQ_GTFS.")
    except Exception as e:
        print("ensure_gtfs_static: failed to download GTFS static on boot:", e)


# Ensure GTFS static exists before loading any CSVs
ensure_gtfs_static()

# Path to the GTFS static data
gtfs_path = "SEQ_GTFS"  # Will need a way to automatically update this.
if gtfs_path is None or not os.path.exists(gtfs_path):
    raise ValueError("GTFS static data path is not set or does not exist, and automatic DownloadOnBoot initialisation failed. Please check GTFS_STATIC_URL / DownloadOnBoot.")

# Load GTFS data into pandas DataFrames
routes = pd.read_csv(os.path.join(gtfs_path, "routes.txt"))
trips = pd.read_csv(os.path.join(gtfs_path, "trips.txt"))
stops = pd.read_csv(os.path.join(gtfs_path, "stops.txt"))
stop_times = pd.read_csv(os.path.join(gtfs_path, "stop_times.txt"))

# precompute a fast mapping from stop_id -> set(route_id) to avoid heavy pandas work inside the async loop
try:
    # stop_times typically doesn't include route_id; join via trips.trip_id -> route_id
    if 'trip_id' in stop_times.columns and 'route_id' in trips.columns:
        _stops_routes = stop_times.merge(trips[['trip_id','route_id']], on='trip_id', how='left')
        # normalize stop_id and route_id to strings trimmed of whitespace
        _stops_routes['stop_id'] = _stops_routes['stop_id'].astype(str).str.strip()
        _stops_routes['route_id'] = _stops_routes['route_id'].astype(str).str.strip()
        stop_to_routes = _stops_routes.groupby('stop_id')['route_id'].apply(lambda s: set(s.dropna())).to_dict()
    else:
        stop_to_routes = {}
except Exception as e:
    print('warning building stop->routes map:', e)
    stop_to_routes = {}

# Create lookup dictionaries for faster data access
route_lookup = routes.set_index("route_id")[["route_short_name", "route_long_name", "route_color"]].to_dict("index")
trip_lookup = trips.set_index("trip_id")["trip_headsign"].to_dict()
stop_names = stops["stop_name"].tolist()

# Set up Discord bot intents and command prefix
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def resolve_stop_input(user_input: str):
    """Resolves user input to a stop name and a list of relevant stop IDs."""
    # Standardize input and strip common suffixes
    cleaned_user_input = user_input.strip()
    if cleaned_user_input.lower().endswith(' station'):
        cleaned_user_input = cleaned_user_input[:-8].strip()
    user_input_lower = cleaned_user_input.lower()

    # --- Match Finding ---
    # 1. Exact match for stop_id
    match = stops[stops["stop_id"] == cleaned_user_input]
    # 2. Exact match for stop_name (case-insensitive)
    if match.empty:
        match = stops[stops["stop_name"].str.lower() == user_input_lower]
    # 3. Fuzzy match for stop_name
    if match.empty:
        fuzzy_matches = stops[stops["stop_name"].str.contains(cleaned_user_input, case=False, na=False)]
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
    # The most reliable way to handle parent stations is to directly look for child stops.
    child_stops = stops[stops['parent_station'] == stop_id]

    if not child_stops.empty:
        # This is a parent station. Return the list of its child platform IDs.
        return stop_name, child_stops['stop_id'].tolist(), True
    else:
        # This is a single platform or a parent with no listed children. Use its own ID.
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
    # start background task to update the Translink status message every STATUS_UPDATE_INTERVAL seconds
    bot.loop.create_task(network_status_loop())

    # start daily GTFS static auto-update task (midnight + optional DownloadOnBoot)
    try:
        bot.loop.create_task(track.auto_update_gtfs_daily())
        print("started auto_update_gtfs_daily task")
    except Exception as e:
        print("failed to start auto_update_gtfs_daily:", e)

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
    stop_name_input = stop_name
    stop_name, stop_ids, is_station = resolve_stop_input(stop_name_input)
    print(f"Resolved '{stop_name_input}' to stop_name: '{stop_name}', stop_ids: {stop_ids}, is_station: {is_station}")
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
            title=f"No upcoming departures at {stop_name}",
            description="There are no services scheduled for this stop at this time.",
            color=discord.Color.orange()
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
        title=f"Next Departures at {stop_name}",
        color=embed_color
    )
    embed.description = f"```ansi\n{header}\n" + "\n".join(rows) + "\n```"
    embed.set_footer(text=f"Last updated: {now.strftime('%H:%M:%S')} | Mode: {pid_mode.capitalize()}", icon_url="https://seqta.org/seqta.png")
    
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
                        embed.set_footer(text=f"Last updated: {now.strftime('%H:%M:%S')}", icon_url="https://seqta.org/seqta.png")

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
        embed.set_footer(text=f"Made with ❤️", icon_url="https://seqta.org/seqta.png")

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
    # If the track module is configured to download on boot, do that before starting the bot.
    try:
        if getattr(track, "DOWNLOAD_ON_BOOT", False):
            print("pre-launch: DownloadOnBoot is enabled; updating GTFS static before starting bot...")
            track.update_gtfs_now_blocking("DownloadOnBoot pre-launch")
    except Exception as e:
        print("pre-launch: GTFS DownloadOnBoot update failed:", e)

    bot.run(TOKEN)
@bot.tree.command(name="post_status", description="(debug) Immediately post or update the Translink status message in the configured STATUS_CHANNEL_ID.")
async def post_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not STATUS_CHANNEL_ID:
        await interaction.followup.send("STATUS_CHANNEL_ID is not configured on the bot.", ephemeral=True)
        return
    try:
        channel = bot.get_channel(STATUS_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(STATUS_CHANNEL_ID)
    except Exception as e:
        await interaction.followup.send(f"Could not access configured channel: {e}", ephemeral=True)
        return

    try:
        settings = await fetch_status_settings()
        content, debug_content = await build_status_message(settings=settings)
        # try to find existing main and debug messages
        last_message = None
        last_debug_message = None
        try:
            async for m in channel.history(limit=200):
                if m.author != bot.user or not isinstance(m.content, str):
                    continue
                if m.content.startswith("# Translink Status:") and last_message is None:
                    last_message = m
                elif m.content.startswith("## Status Debug:") and last_debug_message is None:
                    last_debug_message = m
                if last_message and last_debug_message:
                    break
        except Exception:
            last_message = last_message or None
            last_debug_message = last_debug_message or None

        # main status message
        if last_message:
            await last_message.edit(content=content)
            main_msg_action = "Edited existing status message."
        else:
            await channel.send(content)
            main_msg_action = "Sent new status message."

        # optional debug/notice-sources message
        debug_msg_action = ""
        if debug_content:
            try:
                chunks = split_message(debug_content, max_len=1990)
                if chunks:
                    # First chunk edits or creates the primary debug message
                    first_chunk = chunks[0]
                    if last_debug_message:
                        await last_debug_message.edit(content=first_chunk)
                        debug_msg_action = " Edited existing debug status message."
                    else:
                        await channel.send(first_chunk)
                        debug_msg_action = " Sent new debug status message."

                    # Remaining chunks are sent as additional messages
                    for idx, extra_chunk in enumerate(chunks[1:], start=2):
                        await channel.send(extra_chunk)
                        debug_msg_action += f" Sent extra debug status chunk {idx}."
            except Exception as e:
                debug_msg_action = f" Failed to send/edit debug status message: {e}"

        await interaction.followup.send(main_msg_action + debug_msg_action, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to post status: {e}", ephemeral=True)