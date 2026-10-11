#!/usr/bin/env python3
"""Fieldcard: turn "I have 90 minutes" into a one-page printed field card.

Open pieces that make it work:
  * an open-weight model served locally by Ollama (default: llama3.2:3b; try gemma2:2b, qwen2.5:3b)
    writes the mission + "notice nine things" bingo grounded in your real terrain.
  * OpenStreetMap (Overpass) supplies real nearby parks, water, viewpoints, and peaks.
  * iNaturalist provides real flora and fauna species observed nearby this month.
  * sun and season math is plain code (NOAA solar equations), never left to the model.

Design rule: the LLM writes the poetry, the code does the safety math.
If Ollama or the network is missing, Fieldcard still produces a complete, sensory card.

Usage:
  python fieldcard.py --lat 40.7678 --lon -73.9718 --minutes 90 --activity walk
  python fieldcard.py serve --port 8000
  python fieldcard.py log --found "1, 4, 7" --notes "Saw a red-tailed hawk above the reservoir."
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import random
import re
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Tuple, Any

try:
    import qr_code
except ImportError:
    qr_code = None

SPEEDS_KMH = {"walk": 4.5, "run": 9.0, "birding": 2.5, "garden": 0.0}
DETOUR = 1.3  # straight-line distance -> real trail distance factor
OVERPASS_MIRRORS = [
    os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    "https://lz4.overpass-api.de/api/interpreter",
]
DEFAULT_MODEL = os.environ.get("FIELDCARD_MODEL", "llama3.2:3b")
ACCENT = {
    "spring": "#3f7d3a",
    "summer": "#b8860b",
    "autumn": "#c4551d",
    "winter": "#3b6a8f",
    "tropical": "#1f8a70",
}


# ---------------------------------------------------------------- geometry


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg: float) -> str:
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int((deg + 22.5) // 45) % 8]


# ---------------------------------------------------------------- sun + season


def sun_events(day: dt.date, lat: float, lon: float, tz_hours: float) -> Dict[str, Optional[dt.datetime]]:
    """NOAA solar equations. Returns naive local datetimes (or None at polar extremes)."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g)
        + 0.00148 * math.sin(3 * g)
    )
    lr = math.radians(lat)

    def event(zenith: float, rising: bool) -> Optional[dt.datetime]:
        c = (math.cos(math.radians(zenith)) - math.sin(lr) * math.sin(decl)) / (
            math.cos(lr) * math.cos(decl)
        )
        if not -1 <= c <= 1:
            return None
        h = math.degrees(math.acos(c))
        minutes = 720 - 4 * (lon + (h if rising else -h)) - eqt + tz_hours * 60
        return dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=minutes)

    return {
        "sunrise": event(90.833, True),
        "sunset": event(90.833, False),
        "dusk": event(96.0, False),
    }


def season_for(lat: float, month: int) -> str:
    if abs(lat) < 23.5:
        return "tropical"
    if lat < 0:
        month = (month + 5) % 12 + 1  # flip hemisphere
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def fmt_t(t: Optional[dt.datetime]) -> str:
    if t is None:
        return "--"
    h = t.hour
    return f"{h % 12 or 12}:{t.minute:02d} {'am' if h < 12 else 'pm'}"


# ---------------------------------------------------------------- places (OpenStreetMap)


def reach_km(activity: str, minutes: int) -> float:
    """Straight-line radius reachable with return journey inside time budget."""
    return SPEEDS_KMH[activity] * (minutes / 60.0) / 2.0 / DETOUR


def _kind(tags: Dict[str, str]) -> str:
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    nat = tags.get("natural")
    if nat in ("peak", "water", "wood"):
        return {"water": "water", "peak": "peak", "wood": "woods"}[nat]
    if "waterway" in tags:
        return "stream"
    leis = tags.get("leisure", "")
    return {"nature_reserve": "reserve"}.get(leis, leis or "place")


def parse_overpass(data: Dict[str, Any], lat: float, lon: float) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        c = el.get("center") or el
        if "lat" not in c or "lon" not in c:
            continue
        kind = _kind(tags)
        name = tags.get("name") or ("Viewpoint" if kind == "viewpoint" else "")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "kind": kind, "lat": c["lat"], "lon": c["lon"]})
    return out


def fetch_places(lat: float, lon: float, radius_m: int, cache_dir: Optional[str] = None, timeout: int = 8) -> List[Dict[str, Any]]:
    key = hashlib.sha1(f"{lat:.3f},{lon:.3f},{int(radius_m)}".encode()).hexdigest()[:12]
    cache_dir = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "fieldcard")
    path = os.path.join(cache_dir, key + ".json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Use nw (nodes and ways) instead of nwr to avoid heavy relation assembly timeouts
    q = (
        f"[out:json][timeout:{timeout}];("
        f'nw["leisure"~"park|nature_reserve|garden"]["name"](around:{radius_m},{lat},{lon});'
        f'nw["tourism"="viewpoint"](around:{radius_m},{lat},{lon});'
        f'nw["natural"~"peak|water|wood"]["name"](around:{radius_m},{lat},{lon});'
        f'nw["waterway"~"river|stream"]["name"](around:{radius_m},{lat},{lon});'
        f");out center 50;"
    )
    data_encoded = urllib.parse.urlencode({"data": q}).encode()

    places = []
    for mirror in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(
                mirror,
                data=data_encoded,
                headers={"User-Agent": "fieldcard/1.0 (hackathon open-source-ai)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                places = parse_overpass(json.load(r), lat, lon)
                if places:
                    break
        except Exception:
            continue

    # Rural auto-expansion: if 0 places and radius is modest, try once at 1.8x up to 3500m
    if not places and radius_m < 2000:
        expanded_m = min(int(radius_m * 1.8), 3500)
        q_exp = (
            f"[out:json][timeout:{timeout}];("
            f'nw["leisure"~"park|nature_reserve|garden"]["name"](around:{expanded_m},{lat},{lon});'
            f'nw["tourism"="viewpoint"](around:{expanded_m},{lat},{lon});'
            f'nw["natural"~"peak|water|wood"]["name"](around:{expanded_m},{lat},{lon});'
            f");out center 50;"
        )
        try:
            req = urllib.request.Request(
                OVERPASS_MIRRORS[0],
                data=urllib.parse.urlencode({"data": q_exp}).encode(),
                headers={"User-Agent": "fieldcard/1.0 (hackathon open-source-ai)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                places = parse_overpass(json.load(r), lat, lon)
        except Exception:
            pass

    if places:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(places, f)
        except Exception:
            pass
    return places


def pick_places(raw: List[Dict[str, Any]], lat: float, lon: float, activity: str, minutes: int, limit: int = 5) -> List[Dict[str, Any]]:
    """Select reachable places with diverse terrain types."""
    rk = reach_km(activity, minutes)
    cands = []
    for p in raw:
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if 0.05 <= d <= rk:
            b = bearing_deg(lat, lon, p["lat"], p["lon"])
            mins = d * DETOUR / SPEEDS_KMH[activity] * 60
            cands.append({**p, "dist": d, "bearing": b, "dir": compass(b), "mins": mins})
    cands.sort(key=lambda p: p["dist"])
    chosen, kinds = [], set()
    for p in cands:
        if p["kind"] not in kinds and len(chosen) < limit:
            chosen.append(p)
            kinds.add(p["kind"])
    for p in cands:
        if p not in chosen and len(chosen) < limit:
            chosen.append(p)
    return sorted(chosen, key=lambda p: p["dist"])


# ---------------------------------------------------------------- iNaturalist open species data


def fetch_species(lat: float, lon: float, month: int, radius_km: float = 6.0, timeout: int = 3,
                  cache_dir: Optional[str] = None) -> List[str]:
    """Query iNaturalist open API for local species seen this month. Cached and non-blocking."""
    key = hashlib.sha1(f"species_{lat:.2f}_{lon:.2f}_{month}".encode()).hexdigest()[:12]
    cache_dir = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "fieldcard")
    path = os.path.join(cache_dir, f"{key}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    url = (
        f"https://api.inaturalist.org/v1/observations/species_counts?"
        f"lat={lat:.4f}&lng={lon:.4f}&radius={radius_km:.1f}&month={month}"
        f"&quality_grade=research&per_page=5"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "fieldcard/1.0 (hackathon-project)"})
    species = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
            for item in data.get("results", []):
                taxon = item.get("taxon", {})
                name = taxon.get("preferred_common_name") or taxon.get("name")
                if name and name not in species:
                    species.append(name.strip())
        if species:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(species, f)
    except Exception:
        species = []
    return species[:4]


# ---------------------------------------------------------------- weather (Open-Meteo, free, no key)

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 71: "Light snow", 73: "Snow",
    75: "Heavy snow", 80: "Rain showers", 81: "Moderate showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Severe thunderstorm",
}

WEATHER_EMOJI = {
    0: "☀️", 1: "🌤", 2: "⛅", 3: "☁️", 45: "🌫", 48: "🌫", 51: "🌦", 53: "🌧",
    55: "🌧", 61: "🌦", 63: "🌧", 65: "🌧", 71: "🌨", 73: "❄️", 75: "❄️",
    80: "🌦", 81: "🌧", 82: "⛈", 95: "⛈", 96: "⛈", 99: "⛈",
}


def fetch_weather(lat: float, lon: float, timeout: int = 3) -> Optional[Dict[str, Any]]:
    """Fetch current weather from Open-Meteo (free, no API key). Returns None on failure."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat:.4f}&longitude={lon:.4f}"
        f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
        f"&temperature_unit=celsius&wind_speed_unit=kmh"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fieldcard/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
            current = data.get("current", {})
            code = current.get("weather_code", 0)
            return {
                "temp_c": current.get("temperature_2m"),
                "humidity": current.get("relative_humidity_2m"),
                "wind_kmh": current.get("wind_speed_10m"),
                "code": code,
                "desc": WMO_CODES.get(code, "Unknown"),
                "emoji": WEATHER_EMOJI.get(code, "🌡"),
            }
    except Exception:
        return None


# ---------------------------------------------------------------- garden mode logic


def garden_advice(frost_date_str: Optional[str], today: Optional[dt.date] = None,
                  crops_file: str = "garden_crops.json") -> List[Dict[str, str]]:
    """Determine what to sow or tend this week based on relative frost date."""
    today = today or dt.date.today()
    actions = []
    crops_path = os.path.join(os.path.dirname(__file__), crops_file)
    if not os.path.exists(crops_path):
        crops_path = crops_file
    if not os.path.exists(crops_path):
        return [
            {"crop": "Soil care", "action": "Turn compost and mulch bare garden beds with fallen leaves."},
            {"crop": "Observation", "action": "Check undersides of overwintering greens for pests."}
        ]

    try:
        with open(crops_path, "r", encoding="utf-8") as f:
            crop_db = json.load(f)
    except Exception:
        return []

    diff_weeks = 0
    if frost_date_str:
        try:
            frost_d = dt.date.fromisoformat(frost_date_str)
            diff_weeks = int((today - frost_d).days / 7)
        except Exception:
            diff_weeks = 0

    for item in crop_db:
        w_min, w_max = item.get("frost_rel_weeks", [-4, 4])
        if w_min <= diff_weeks <= w_max:
            actions.append({"crop": item["crop"], "action": item["action"]})
    if not actions:
        actions = [
            {"crop": crop_db[0]["crop"], "action": crop_db[0]["action"]},
            {"crop": crop_db[1]["crop"], "action": crop_db[1]["action"]},
        ]
    return actions[:4]


# ---------------------------------------------------------------- copy: fallback + local LLM


SEASONAL: Dict[str, List[str]] = {
    "autumn": [
        "A leaf showing three distinct colours at once",
        "Something that was alive this summer and isn't now",
        "An acorn or seedpod with its cap still on",
        "Bark cold on the north side of a trunk",
        "A spider web catching low afternoon light",
        "Fungus shelf growing horizontal from a rotting log",
        "Dried seedhead that rattles when you flick it",
        "A yellow or red leaf stuck in green grass",
        "Soil that smells like damp cellar or rain",
        "Animal tracks in mud heading toward brush",
    ],
    "spring": [
        "A bud swollen tight like a clenched fist",
        "The first petal of any colour fallen to earth",
        "New leaf so fresh it looks translucent",
        "Patch of mud holding a clear bird or boot print",
        "Moss so lush it feels like a damp sponge",
        "A bird with a twig or dry grass in its beak",
        "Water running clear in a ditch or gutter",
        "Pollen dusting the surface of a puddle",
        "A bee hovering without landing for five seconds",
        "The smell of cold wet earth warmed by sun",
    ],
    "summer": [
        "The deepest shadow within fifty paces",
        "A stone still warm ten minutes after sun moves",
        "An insect working so hard it ignores you",
        "Wild fruit or berry still hard and green",
        "The single spot where air moves against your neck",
        "A leaf with jagged bite marks from a caterpillar",
        "Pine needles dry enough to snap between fingers",
        "A tiny fish or water strider moving in shallows",
        "The steady drone of cicadas or bees overhead",
        "Your own footprint disappearing in dry dust",
    ],
    "winter": [
        "A bare branch with next spring's bud already formed",
        "Hollow sound of dry frozen grass underfoot",
        "Your own breath turning to mist before vanishing",
        "Evergreen needle: crush it between fingers and smell",
        "A bird puffed into a ball against the wind",
        "Frost crystals lining the serrated edge of a leaf",
        "Dead thistle or weed holding a rim of ice",
        "Patch of lichen glowing pale green on gray stone",
        "The lowest angle your shadow can cast on the ground",
        "Animal tracks in grit or frost heading to cover",
    ],
    "tropical": [
        "An ant column marching with pieces twice their size",
        "The heavy, sweet scent of a bloom you cannot see",
        "A tree trunk host to at least three different plants",
        "A raindrop trembling in the crook of a leaf",
        "Hollow knocking sound of bamboo or heavy palm fronds",
        "A lizard frozen motionless waiting for an insect",
        "Soil so dark and rich it stains your thumb",
        "A fallen seed pod larger than your hand",
        "The chill of breeze arriving right before rain",
        "A bird call that sounds like two pebbles tapped together",
    ],
}

UNIVERSAL: List[str] = [
    "A clear sound with no engine or human voice in it",
    "A stone or tree that was here before your grandparents",
    "Three distinct shades of brown, green, or gray side by side",
    "A natural texture you would not have guessed before touching",
    "A feather, shell, or curious pebble small enough to pocket",
    "The exact direction the wind is blowing against your cheek",
]

MISSIONS: Dict[str, str] = {
    "walk": "Walk at an easy conversational pace to your farthest waypoint. Stop once for two silent minutes with eyes closed to map the sounds around you. Loop home by a different path if you can.",
    "run": "Keep your stride conversational and light. Run out smoothly to your farthest turn, then walk the final five minutes home to let your pulse settle.",
    "birding": "Move like water: slow, deliberate, quiet. Pause for three silent minutes at every waypoint. Count what you hear before you scan for what moves in the branches.",
    "garden": "Step outside without your phone. Press your thumb an inch into the soil to test the moisture. Inspect the underside of five leaves, and weed or trim one small patch with care.",
    "group": "Gather at the start. Run or walk in pace groups: conversational, steady, or tempo. Reconnect at the turnaround to share what you noticed.",
}

BANNED_WORDS = [
    "embrace", "journey", "tapestry", "immerse", "unplug",
    "nature's beauty", "breathtaking", "serene", "vibrant", "nestled", "delve"
]


def fallback_copy(facts: Dict[str, Any], rng: random.Random) -> Dict[str, Any]:
    season_items = SEASONAL.get(facts["season"], SEASONAL["autumn"])
    items = rng.sample(season_items, min(6, len(season_items))) + rng.sample(UNIVERSAL, 3)
    rng.shuffle(items)
    mission_key = "group" if facts.get("group") else facts["activity"]
    return {
        "title": f"{facts['minutes']} minutes outside",
        "mission": MISSIONS.get(mission_key, MISSIONS["walk"]),
        "notice": items[:9],
        "send_off": "Phone in pocket. Shoulders down. Eyes open.",
        "source": "built-in prompts",
    }


def _clip(s: Any, words: int) -> str:
    w = str(s).replace("\n", " ").split()
    return " ".join(w[:words])


def _contains_banned(s: str) -> bool:
    low = s.lower()
    return any(b in low for b in BANNED_WORDS)


def parse_llm_json(text: str, fallback: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate model output; strictly clip lengths, check schema, fall back field by field."""
    try:
        # Extract json object if model included extra text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        d = json.loads(match.group(0))
        assert isinstance(d, dict)
    except Exception:
        return None

    out = dict(fallback)
    if isinstance(d.get("title"), str) and d["title"].strip():
        val = _clip(d["title"], 7)
        if not _contains_banned(val):
            out["title"] = val

    if isinstance(d.get("mission"), str) and d["mission"].strip():
        val = _clip(d["mission"], 42)
        if not _contains_banned(val):
            out["mission"] = val

    if isinstance(d.get("send_off"), str) and d["send_off"].strip():
        val = _clip(d["send_off"], 14)
        if not _contains_banned(val):
            out["send_off"] = val

    raw_notice = d.get("notice", [])
    if isinstance(raw_notice, list):
        cleaned = [
            _clip(x, 10) for x in raw_notice
            if isinstance(x, str) and x.strip() and not _contains_banned(str(x))
        ]
        if len(cleaned) >= 5:
            cleaned += fallback["notice"]
            out["notice"] = cleaned[:9]

    out["source"] = "local model"
    return out


def llm_copy(facts: Dict[str, Any], places: List[Dict[str, Any]], fallback: Dict[str, Any],
             model: str = DEFAULT_MODEL, host: Optional[str] = None, timeout: int = 90) -> Optional[Dict[str, Any]]:
    host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if not host.startswith("http"):
        host = "http://" + host

    place_summaries = "; ".join(
        f"{p['name']} ({p['kind']}, {p['dist']:.1f} km {p['dir']})" for p in places
    ) or "neighborhood trails and open ground"

    few_shot_1 = (
        'Example 1:\n'
        'Facts: season=autumn; activity=walk; time=60 min; Nearby: Ramble (woods, 0.4 km NW); The Lake (water, 0.6 km W)\n'
        'Output: {"title": "Sixty Minutes in the Oaks", '
        '"mission": "Follow the earthen trail into the Ramble. Stand motionless beside the water until the ripple circles vanish.", '
        '"notice": ["A yellow oak leaf pinned in mud", "Spider web strung between low twigs", '
        '"Acorn with its rough cap intact", "Fungus like white stairs on bark", '
        '"Cold stone on the shadowed bank", "Bird with a berry in its beak", '
        '"Tree root curling across the dirt", "Sound of dry grass brushing ankles", '
        '"Bark smelling like wet cellar"], '
        '"send_off": "Phone in pocket. Shoulders down."}'
    )

    prompt = (
        "You write the text for a one-page PRINTED outdoor field card.\n"
        "Tone: Sensory, physical, warm, concrete. Plain language. No emojis.\n"
        "Rules: Never invent place names (use ONLY the names given). Never mention times or distances.\n"
        "Never use cliches like: 'embrace', 'journey', 'tapestry', 'immerse', 'unplug', 'nature\\'s beauty'.\n"
        f"{few_shot_1}\n\n"
        f"Now write for:\n"
        f"Season: {facts['season']}. Activity: {facts['activity']}. Time budget: {facts['minutes']} minutes.\n"
        f"Nearby real places: {place_summaries}.\n"
        "Return ONLY a JSON object with keys: title (max 6 words), mission (max 38 words), "
        "notice (exactly 9 sensory items to find with bare hands and eyes, each max 8 words), "
        "send_off (max 12 words)."
    )

    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.7, "top_p": 0.9},
    }).encode("utf-8")

    req = urllib.request.Request(
        host.rstrip("/") + "/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r).get("response", "")
            return parse_llm_json(res, fallback)
    except Exception:
        return None


# ---------------------------------------------------------------- rendering


CSS = """
@page { size: A5 portrait; margin: 6mm 7mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #e9e4d6; font-family: Georgia, 'Times New Roman', serif; color: #1d2b22; }
.card { width: 148mm; min-height: 206mm; max-height: 208mm; margin: 6mm auto; background: #f8f4e8; padding: 7mm 8mm;
 border: 1px solid #cfc7b0; position: relative; page-break-inside: avoid; break-inside: avoid; }
@media print {
  body { background: none; }
  .card { margin: 0; border: 0; width: 100%; min-height: 0; padding: 0; max-height: none; }
  .web-bar { display: none !important; }
}
.web-bar { width: 148mm; margin: 6mm auto 0; display: flex; justify-content: space-between; align-items: center; }
.btn-print { background: #1d2b22; color: #f8f4e8; border: none; padding: 7px 18px; font: 700 10pt Helvetica, sans-serif;
 border-radius: 4px; cursor: pointer; }
.btn-print:hover { background: #384d3e; }
.btn-back { color: #1d2b22; font: 600 10pt Helvetica, sans-serif; text-decoration: none; }
.btn-back:hover { text-decoration: underline; }
.kicker { font: 700 7.5pt/1 Helvetica, Arial, sans-serif; letter-spacing: .16em; text-transform: uppercase; color: var(--a); }
h1 { font-size: 21pt; line-height: 1.05; margin: 2mm 0 3mm; letter-spacing: -0.02em; }
.stats { display: flex; gap: 2mm; margin-bottom: 3mm; }
.stat { flex: 1; border: 1.2px solid #1d2b22; padding: 1.8mm 2.2mm; background: #fffdf8; }
.stat b { display: block; font: 700 6.5pt Helvetica, Arial, sans-serif; letter-spacing: .12em; text-transform: uppercase; color: var(--a); }
.stat span { font-size: 12pt; font-weight: 600; }
.warn { background: #fbe3d3; border-left: 3mm solid var(--a); padding: 1.8mm 2.5mm; margin-bottom: 3mm; font-size: 9pt; }
.mission { font-size: 11.5pt; line-height: 1.32; margin: 0 0 3mm; }
.mid { display: flex; gap: 3.5mm; margin-bottom: 3mm; align-items: flex-start; }
.mid svg { width: 50mm; height: 50mm; flex: none; }
ol.legend { margin: 0; padding-left: 4.5mm; font-size: 8.5pt; line-height: 1.3; }
ol.legend small { display: block; color: #5b6b60; font-size: 7.5pt; }
.rule { border: 1.2px dashed #1d2b22; padding: 2.5mm; font-size: 9.5pt; flex: 1; }
.species-bar { background: #eef3ee; border: 1px solid #c2d6c3; padding: 1.5mm 2.5mm; margin-bottom: 2.5mm;
 font-size: 8pt; line-height: 1.25; }
.species-bar b { color: #3f7d3a; text-transform: uppercase; font-size: 7pt; letter-spacing: .08em; }
.garden-box { border: 1.2px solid #1d2b22; padding: 2mm 3mm; margin-bottom: 3mm; background: #faf8f0; }
.garden-box h2 { margin: 0 0 1.5mm; font-size: 7.5pt; }
.garden-item { font-size: 8.5pt; margin-bottom: 1.2mm; }
.garden-item b { color: var(--a); }
.group-box { display: flex; gap: 3mm; border: 1.2px solid #1d2b22; padding: 2mm 3mm; margin-bottom: 3mm; align-items: center; }
.group-routes { flex: 1; font-size: 8pt; line-height: 1.3; }
.group-qr { flex: none; width: 28mm; height: 28mm; }
h2 { font: 700 7.5pt Helvetica, Arial, sans-serif; letter-spacing: .16em; text-transform: uppercase; margin: 0 0 1.8mm; color: var(--a); }
.bingo { display: flex; flex-wrap: wrap; gap: 1.2mm; }
.cell { width: calc(33.333% - 0.8mm); border: 1.2px solid #1d2b22; padding: 1.8mm; min-height: 13.5mm;
 font-size: 8.2pt; line-height: 1.15; position: relative; background: #fffdfa; }
.cell i { position: absolute; right: 1.5mm; bottom: 1.5mm; width: 3.5mm; height: 3.5mm; border: 1.2px solid #1d2b22; }
.foot { margin-top: 3mm; border-top: 1.2px solid #1d2b22; padding-top: 2mm; font-size: 9pt; display: flex; justify-content: space-between; gap: 3mm; }
.tiny { font: 7pt Helvetica, Arial, sans-serif; color: #5b6b60; margin-top: 1.5mm; }
.weather-bar { display: flex; gap: 3mm; background: #f0f4f8; border: 1px solid #c0ccd6; padding: 1.5mm 2.5mm; margin-bottom: 2.5mm;
 font-size: 8pt; line-height: 1.25; align-items: center; }
.weather-bar .wx-emoji { font-size: 14pt; flex: none; }
.weather-bar .wx-detail { flex: 1; }
.weather-bar b { color: #3b6a8f; text-transform: uppercase; font-size: 7pt; letter-spacing: .08em; }
"""


def map_svg(places: List[Dict[str, Any]], rk: float, accent: str) -> str:
    S, c, R = 360, 180, 150
    p = [
        f'<svg viewBox="0 0 {S} {S}" xmlns="http://www.w3.org/2000/svg" font-family="Helvetica,Arial,sans-serif">',
        f'<rect width="{S}" height="{S}" fill="none" stroke="#1d2b22" stroke-width="2"/>',
        f'<circle cx="{c}" cy="{c}" r="{R}" fill="none" stroke="#1d2b22" stroke-width="1.5"/>',
        f'<circle cx="{c}" cy="{c}" r="{R / 2}" fill="none" stroke="#1d2b22" stroke-dasharray="4 4"/>',
        f'<text x="{c}" y="22" text-anchor="middle" font-size="16" font-weight="700">N</text>',
        f'<text x="{c + R - 4}" y="{c - 6}" text-anchor="end" font-size="11">{rk:.1f} km</text>',
        f'<text x="{c + R / 2 - 4}" y="{c - 6}" text-anchor="end" font-size="11">{rk / 2:.1f}</text>',
        f'<rect x="{c - 6}" y="{c - 6}" width="12" height="12" fill="#1d2b22"/>',
        f'<text x="{c + 10}" y="{c + 20}" font-size="11" font-weight="700">START</text>',
    ]
    for i, pl in enumerate(places, 1):
        r = min(pl["dist"] / rk, 1.0) * R
        a = math.radians(pl["bearing"])
        x, y = c + r * math.sin(a), c - r * math.cos(a)
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="{accent}"/>')
        p.append(
            f'<text x="{x:.1f}" y="{y + 4.5:.1f}" text-anchor="middle" font-size="13" '
            f'font-weight="700" fill="#fff">{i}</text>'
        )
    p.append("</svg>")
    return "".join(p)


def render_card(facts: Dict[str, Any], places: List[Dict[str, Any]], copy: Dict[str, Any],
                sun: Dict[str, Optional[dt.datetime]], start: dt.datetime, end: dt.datetime,
                species: Optional[List[str]] = None, garden_info: Optional[List[Dict[str, str]]] = None,
                web_ui: bool = False, weather: Optional[Dict[str, Any]] = None) -> str:
    e, a = html.escape, ACCENT[facts["season"]]
    mins, act = facts["minutes"], facts["activity"]
    turn = start + dt.timedelta(minutes=mins / 2)

    warn = ""
    if sun["sunset"] and end > sun["sunset"]:
        latest = sun["sunset"] - dt.timedelta(minutes=mins + 10)
        warn = (
            f'<div class="warn"><b>Daylight check:</b> this walk ends after sunset '
            f'({e(fmt_t(sun["sunset"]))}). Start by {e(fmt_t(latest))} to finish in the light, '
            f'or carry a headlamp.</div>'
        )

    mid = ""
    group_html = ""
    if facts.get("group") and qr_code:
        qr_svg_data = qr_code.qr_svg(f"https://www.openstreetmap.org/#map=15/{facts['lat']:.4f}/{facts['lon']:.4f}", size_px=85)
        group_html = (
            f'<div class="group-box">'
            f'<div class="group-routes">'
            f'<h2>Pace options</h2>'
            f'<b>Conversational:</b> Turn around at {e(fmt_t(turn))}. Stay together.<br>'
            f'<b>Steady Tempo:</b> Farthest waypoint and return. Regroup at start.<br>'
            f'<b>Long Route:</b> Extended loop. Tell lead before heading out.'
            f'</div>'
            f'<div class="group-qr">{qr_svg_data}</div>'
            f'</div>'
        )

    if places:
        legend = "".join(
            f'<li>{e(p["name"])}<small>{e(p["kind"])} · ~{p["dist"]:.1f} km {e(p["dir"])} · '
            f'≈{p["mins"]:.0f} min each way</small></li>'
            for p in (places[:3] if facts.get("group") else places)
        )
        mid = (
            f'<div class="mid">{map_svg(places[:3] if facts.get("group") else places, max(p["dist"] for p in places) * 1.15, a)}'
            f'<ol class="legend">{legend}</ol></div>'
        )
    elif act == "garden":
        if garden_info:
            items_html = "".join(
                f'<div class="garden-item"><b>{e(g["crop"])}:</b> {e(g["action"])}</div>'
                for g in garden_info
            )
            mid = f'<div class="garden-box"><h2>What to do this week</h2>{items_html}</div>'
    else:
        mid = (
            f'<div class="mid"><div class="rule"><h2>No map data? Use the rule</h2>'
            f'Head out with the sun behind you. Turn around at <b>{e(fmt_t(turn))}</b> '
            f'wherever you are, no matter how good the view.</div></div>'
        )

    species_html = ""
    if species:
        species_html = f'<div class="species-bar"><b>Seen nearby this month:</b> {e(", ".join(species))}</div>'

    weather_html = ""
    if weather and weather.get("temp_c") is not None:
        wx = weather
        wind_note = f' · Wind {wx["wind_kmh"]:.0f} km/h' if wx.get('wind_kmh') else ''
        weather_html = (
            f'<div class="weather-bar">'
            f'<span class="wx-emoji">{wx["emoji"]}</span>'
            f'<div class="wx-detail"><b>Right now</b> '
            f'{wx["temp_c"]:.0f}°C · {e(wx["desc"])}{wind_note}</div>'
            f'</div>'
        )

    cells = "".join(f'<div class="cell">{e(n)}<i></i></div>' for n in copy["notice"][:9])
    stats = (
        f'<div class="stat"><b>Out for</b><span>{mins} min</span></div>'
        + ('' if act == "garden" else f'<div class="stat"><b>Turn around</b><span>{e(fmt_t(turn))}</span></div>')
        + f'<div class="stat"><b>Sunset</b><span>{e(fmt_t(sun["sunset"]))}</span></div>'
    )

    web_bar = ""
    if web_ui:
        web_bar = (
            '<div class="web-bar">'
            '<button onclick="window.print()" class="btn-print">Print Field Card (A5)</button>'
            '<a href="/" class="btn-back">← Make another card</a>'
            '</div>'
        )

    return (
        f'<!doctype html><html lang="en"><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Fieldcard · {e(copy["title"])}</title>'
        f'<style>{CSS}</style><body style="--a:{a}">'
        f'{web_bar}'
        f'<div class="card">'
        f'<div class="kicker">Field card · {e(facts["date"])} · {e(act)} · {e(facts["season"])}</div>'
        f'<h1>{e(copy["title"])}</h1><div class="stats">{stats}</div>{warn}'
        f'<p class="mission">{e(copy["mission"])}</p>{group_html}{mid}{weather_html}{species_html}'
        f'<h2>Notice nine things</h2><div class="bingo">{cells}</div>'
        f'<div class="foot"><div><b>{e(copy["send_off"])}</b><br>Back by {e(fmt_t(end))}. '
        f'Tell someone where you are going.</div></div>'
        f'<div class="tiny">Text: {e(copy["source"])} · Places: OpenStreetMap contributors · '
        f'Distances are straight-line estimates; trust the ground, not the card.</div>'
        f'</div></body></html>'
    )


# ---------------------------------------------------------------- main build pipeline


def build(args: Any) -> str:
    day = dt.date.fromisoformat(args.date) if getattr(args, "date", None) else dt.date.today()
    tz = args.tz if getattr(args, "tz", None) is not None else (
        dt.datetime.now().astimezone().utcoffset().total_seconds() / 3600
    )
    if getattr(args, "start", None):
        hh, mm = map(int, args.start.split(":"))
        start = dt.datetime.combine(day, dt.time(hh, mm))
    else:
        start = dt.datetime.combine(day, dt.datetime.now().time()).replace(second=0, microsecond=0)
    end = start + dt.timedelta(minutes=args.minutes)
    sun = sun_events(day, args.lat, args.lon, tz)
    facts = {
        "date": day.strftime("%a %d %b %Y"),
        "season": season_for(args.lat, day.month),
        "activity": args.activity,
        "minutes": args.minutes,
        "lat": args.lat,
        "lon": args.lon,
        "group": getattr(args, "group", False),
    }

    places = []
    if args.activity != "garden":
        raw = []
        if getattr(args, "places", None):
            with open(args.places, "r", encoding="utf-8") as f:
                raw = json.load(f)
        elif not getattr(args, "no_net", False):
            try:
                raw = fetch_places(
                    args.lat,
                    args.lon,
                    int(reach_km(args.activity, args.minutes) * 1000),
                )
            except Exception as ex:
                print(f"[fieldcard] no map data ({ex.__class__.__name__}); using offline rule", file=sys.stderr)
        places = pick_places(raw, args.lat, args.lon, args.activity, args.minutes)

    species = []
    if not getattr(args, "no_net", False) and not getattr(args, "no_species", False):
        try:
            species = fetch_species(args.lat, args.lon, day.month)
        except Exception:
            species = []

    garden_info = None
    if args.activity == "garden":
        frost_date = getattr(args, "frost_date", None)
        garden_info = garden_advice(frost_date, today=day)

    rng = random.Random(f"{day}{args.lat:.2f}{args.lon:.2f}{args.activity}")
    copy = fallback_copy(facts, rng)
    if not getattr(args, "no_llm", False):
        model_name = getattr(args, "model", DEFAULT_MODEL)
        got = llm_copy(facts, places, copy, model=model_name)
        if got:
            copy = got
        else:
            print("[fieldcard] local model unreachable; using built-in prompts "
                  "(is `ollama serve` running?)", file=sys.stderr)

    web_ui = getattr(args, "web", False)

    # Fetch current weather (Open-Meteo, free, no key) — graceful offline fallback
    weather = None
    if not getattr(args, "no_net", False):
        try:
            weather = fetch_weather(args.lat, args.lon)
        except Exception:
            weather = None

    rendered = render_card(facts, places, copy, sun, start, end, species=species,
                           garden_info=garden_info, web_ui=web_ui, weather=weather)

    # Save to offline cache
    try:
        cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "fieldcard")
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "last_card.html"), "w", encoding="utf-8") as f:
            f.write(rendered)
    except Exception:
        pass

    return rendered


# ---------------------------------------------------------------- post-walk journal log


def run_log(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Record your field observations in your personal seasons journal.")
    parser.add_argument("--found", help="Comma-separated items noticed, or indices (e.g. '1, 4, 7')")
    parser.add_argument("--notes", help="One-line field observation note")
    parser.add_argument("--file", default="fieldnotes.md", help="Journal file to append to")
    args = parser.parse_args(argv)

    now = dt.datetime.now()
    date_str = now.strftime("%Y-%m-%d · %A")
    found_items = []
    if args.found:
        found_items = [f.strip() for f in args.found.split(",") if f.strip()]

    note_text = args.notes or "Stepped outside. Phone stayed in pocket."

    entry_lines = [
        f"\n## {date_str}",
        f"- **Time**: {now.strftime('%I:%M %p')}",
    ]
    if found_items:
        entry_lines.append(f"- **Noticed**: {', '.join(found_items)}")
    entry_lines.append(f"- **Field note**: {note_text}")

    with open(args.file, "a", encoding="utf-8") as f:
        f.write("\n".join(entry_lines) + "\n")

    print(f"Logged field note to {args.file}:")
    for line in entry_lines:
        print(f"  {line}")
    return 0


# ---------------------------------------------------------------- web server


FORM_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fieldcard · Print a Card, Step Outside</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Playfair+Display:wght@700;800&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0f1410; --surface: rgba(26,36,28,0.85); --surface-2: rgba(38,52,40,0.7);
  --border: rgba(63,125,58,0.25); --border-glow: rgba(63,125,58,0.5);
  --text: #e8efe9; --text-dim: #8fa894;
  --accent: #4ade80; --accent-2: #22c55e; --accent-warm: #f59e0b;
  --radius: 16px;
}
body {
  min-height: 100vh; background: var(--bg);
  background-image: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(63,125,58,0.15), transparent),
    radial-gradient(ellipse 60% 40% at 80% 100%, rgba(34,197,94,0.08), transparent);
  color: var(--text); font-family: 'Inter', -apple-system, sans-serif; line-height: 1.5; overflow-x: hidden;
}
.particles { position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }
.particle {
  position: absolute; width: 3px; height: 3px; background: var(--accent);
  border-radius: 50%; opacity: 0.15; animation: float-up linear infinite;
}
@keyframes float-up {
  0% { transform: translateY(100vh) scale(0); opacity: 0; }
  10% { opacity: 0.15; } 90% { opacity: 0.15; }
  100% { transform: translateY(-10vh) scale(1); opacity: 0; }
}
.container { position: relative; z-index: 1; max-width: 520px; margin: 0 auto; padding: 24px 16px 40px; }
.header { text-align: center; margin-bottom: 28px; animation: fadeInDown 0.6s ease; }
@keyframes fadeInDown { from { opacity:0; transform:translateY(-20px); } to { opacity:1; transform:translateY(0); } }
.logo {
  font-family: 'Playfair Display', Georgia, serif; font-size: 42px; font-weight: 800; letter-spacing: -0.03em;
  background: linear-gradient(135deg, var(--accent), #86efac);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}
.tagline { font-size: 14px; color: var(--text-dim); margin-top: 4px; }
.streak-bar { display: flex; gap: 12px; margin-bottom: 20px; animation: fadeInUp 0.7s ease 0.1s both; }
@keyframes fadeInUp { from { opacity:0; transform:translateY(16px); } to { opacity:1; transform:translateY(0); } }
.streak-card, .badges-card {
  background: var(--surface); backdrop-filter: blur(12px); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 16px; transition: border-color 0.3s, transform 0.2s;
}
.streak-card:hover, .badges-card:hover { border-color: var(--border-glow); transform: translateY(-2px); }
.streak-card { flex: 1; text-align: center; }
.badges-card { flex: 1.2; }
.streak-number {
  font-size: 36px; font-weight: 800; font-family: 'Playfair Display', Georgia, serif;
  background: linear-gradient(135deg, var(--accent-warm), #fbbf24);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; line-height: 1;
}
.streak-label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.1em; font-weight: 600; margin-top: 4px; }
.streak-fire { font-size: 20px; display: block; margin-bottom: 2px; }
.badges-title { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.1em; font-weight: 600; margin-bottom: 8px; }
.badges-row { display: flex; gap: 6px; flex-wrap: wrap; }
.badge { display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; transition: all 0.3s; }
.badge.earned { background: rgba(74,222,128,0.15); border: 1px solid rgba(74,222,128,0.3); color: var(--accent); }
.badge.locked { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); color: var(--text-dim); opacity: 0.5; }
.badge .badge-icon { font-size: 14px; }
.status-row { display: flex; gap: 8px; margin-bottom: 20px; animation: fadeInUp 0.7s ease 0.2s both; }
.status-pill {
  flex: 1; display: flex; align-items: center; gap: 8px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 10px 14px; font-size: 12px;
  transition: border-color 0.3s;
}
.status-pill:hover { border-color: var(--border-glow); }
.status-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.status-dot.online { background: var(--accent); box-shadow: 0 0 6px var(--accent); animation: pulse-dot 2s infinite; }
.status-dot.offline { background: #ef4444; }
@keyframes pulse-dot { 0%,100% { opacity:1; } 50% { opacity:0.5; } }
.status-label { font-weight: 600; text-transform: uppercase; font-size: 9px; letter-spacing: 0.08em; color: var(--text-dim); display: block; }
.status-value { font-weight: 500; color: var(--text); }
.weather-pill {
  display: flex; align-items: center; gap: 10px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 10px 14px; margin-bottom: 20px; animation: fadeInUp 0.7s ease 0.25s both;
  transition: border-color 0.3s;
}
.weather-pill:hover { border-color: var(--border-glow); }
.wx-icon { font-size: 28px; flex: none; }
.wx-info { flex: 1; }
.wx-temp { font-size: 18px; font-weight: 700; }
.wx-desc { font-size: 12px; color: var(--text-dim); }
.form-card {
  background: var(--surface); backdrop-filter: blur(12px); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 24px 20px; animation: fadeInUp 0.7s ease 0.3s both;
}
.form-group { margin-bottom: 20px; }
.form-label { display: block; font-weight: 700; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-dim); margin-bottom: 8px; }
.geo-inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }
input[type="number"], input[type="text"], select {
  width: 100%; background: rgba(15,20,16,0.6); border: 1px solid var(--border); border-radius: 10px;
  padding: 10px 14px; color: var(--text); font-size: 14px; font-family: inherit;
  transition: border-color 0.3s, box-shadow 0.3s; outline: none;
}
input:focus, select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(74,222,128,0.1); }
.btn-geo {
  display: block; width: 100%;
  background: linear-gradient(135deg, rgba(74,222,128,0.15), rgba(34,197,94,0.1));
  border: 1px solid rgba(74,222,128,0.3); color: var(--accent); border-radius: 10px;
  padding: 10px 14px; font-weight: 600; font-size: 13px; cursor: pointer;
  transition: all 0.3s; font-family: inherit; margin-bottom: 10px;
}
.btn-geo:hover { background: linear-gradient(135deg, rgba(74,222,128,0.25), rgba(34,197,94,0.2)); transform: translateY(-1px); }
.presets { display: flex; flex-wrap: wrap; gap: 6px; }
.preset-btn {
  background: var(--surface-2); border: 1px solid var(--border); color: var(--text-dim);
  padding: 5px 12px; border-radius: 20px; font-size: 11px; font-weight: 500;
  cursor: pointer; transition: all 0.3s; font-family: inherit;
}
.preset-btn:hover { border-color: var(--accent); color: var(--accent); background: rgba(74,222,128,0.08); }
.slider-row { display: flex; align-items: center; gap: 14px; }
input[type="range"] {
  -webkit-appearance: none; flex: 1; height: 6px; background: rgba(255,255,255,0.08);
  border-radius: 3px; outline: none;
}
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none; width: 22px; height: 22px; background: var(--accent);
  border-radius: 50%; cursor: pointer; box-shadow: 0 0 10px rgba(74,222,128,0.3); transition: transform 0.2s;
}
input[type="range"]::-webkit-slider-thumb:hover { transform: scale(1.15); }
.mins-badge {
  font-family: 'Playfair Display', Georgia, serif; font-size: 22px; font-weight: 700;
  color: var(--accent); width: 80px; text-align: right;
}
.activity-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.activity-card {
  position: relative; border: 1.5px solid var(--border); padding: 14px 12px; border-radius: 12px;
  text-align: center; cursor: pointer; background: rgba(15,20,16,0.4); transition: all 0.3s; overflow: hidden;
}
.activity-card:hover { border-color: var(--border-glow); transform: translateY(-2px); }
.activity-card input { display: none; }
.activity-card:has(input:checked) { border-color: var(--accent); background: rgba(74,222,128,0.08); box-shadow: 0 0 20px rgba(74,222,128,0.1); }
.activity-card input:checked + .act-content { color: var(--accent); }
.act-icon { font-size: 24px; display: block; margin-bottom: 4px; }
.act-name { font-weight: 700; font-size: 14px; }
.btn-submit {
  width: 100%; background: linear-gradient(135deg, var(--accent-2), var(--accent)); color: #0f1410; border: none;
  padding: 16px; font-size: 16px; font-weight: 800; border-radius: 12px; cursor: pointer;
  margin-top: 6px; font-family: 'Inter', sans-serif; letter-spacing: 0.02em; transition: all 0.3s;
  position: relative; overflow: hidden;
}
.btn-submit:hover { transform: translateY(-2px); box-shadow: 0 8px 30px rgba(74,222,128,0.3); }
.btn-submit:active { transform: translateY(0); }
.btn-submit::after {
  content: ''; position: absolute; top: -50%; left: -50%; width: 200%; height: 200%;
  background: linear-gradient(transparent, rgba(255,255,255,0.1), transparent);
  transform: rotate(45deg); animation: shimmer 3s infinite;
}
@keyframes shimmer { 0% { transform: translateX(-100%) rotate(45deg); } 100% { transform: translateX(100%) rotate(45deg); } }
.note { font-size: 11px; color: var(--text-dim); margin-top: 6px; }
.history-section { margin-top: 24px; animation: fadeInUp 0.7s ease 0.4s both; }
.history-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-dim); margin-bottom: 10px; }
.history-list { display: flex; flex-direction: column; gap: 6px; }
.history-item {
  display: flex; align-items: center; gap: 10px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 14px; font-size: 12px; transition: border-color 0.3s;
}
.history-item:hover { border-color: var(--border-glow); }
.history-emoji { font-size: 18px; flex: none; }
.history-info { flex: 1; }
.history-date { color: var(--text-dim); font-size: 11px; }
.history-act { font-weight: 600; color: var(--text); text-transform: capitalize; }
.history-empty {
  text-align: center; padding: 20px; color: var(--text-dim); font-size: 13px;
  background: var(--surface); border: 1px dashed var(--border); border-radius: 10px;
}
@media (max-width: 480px) { .container { padding: 16px 12px 30px; } .streak-bar { flex-direction: column; } .header .logo { font-size: 34px; } }
</style>
</head>
<body>
<div class="particles" id="particles"></div>
<div class="container">
  <div class="header">
    <div class="logo">Fieldcard</div>
    <div class="tagline">Tell us where and for how long. Leave with a page, not an app.</div>
  </div>
  <div class="streak-bar">
    <div class="streak-card">
      <span class="streak-fire" id="streak-fire">🔥</span>
      <div class="streak-number" id="streak-count">0</div>
      <div class="streak-label">Day streak</div>
    </div>
    <div class="badges-card">
      <div class="badges-title">Achievements</div>
      <div class="badges-row" id="badges-row"></div>
    </div>
  </div>
  <div class="status-row">
    <div class="status-pill">
      <div class="status-dot {ollama_dot}"></div>
      <div><span class="status-label">Ollama</span><span class="status-value">{ollama_short}</span></div>
    </div>
    <div class="status-pill">
      <div class="status-dot online"></div>
      <div><span class="status-label">Map Data</span><span class="status-value">{map_status}</span></div>
    </div>
  </div>
  <div class="weather-pill" id="weather-pill" style="display:none">
    <span class="wx-icon" id="wx-icon"></span>
    <div class="wx-info"><span class="wx-temp" id="wx-temp"></span> <span class="wx-desc" id="wx-desc"></span></div>
  </div>
  <div class="form-card">
    <form method="POST" action="/card">
      <div class="form-group">
        <label class="form-label">Location</label>
        <div class="geo-inputs">
          <input type="number" step="any" id="lat" name="lat" placeholder="Latitude" value="40.7678" required>
          <input type="number" step="any" id="lon" name="lon" placeholder="Longitude" value="-73.9718" required>
        </div>
        <button type="button" class="btn-geo" onclick="useLocation()">Use my location</button>
        <div class="presets">
          <button type="button" class="preset-btn" onclick="setPreset(40.7678,-73.9718)">NYC Central Park</button>
          <button type="button" class="preset-btn" onclick="setPreset(51.5072,-0.1657)">London Hyde Park</button>
          <button type="button" class="preset-btn" onclick="setPreset(-37.8304,144.9796)">Melbourne</button>
          <button type="button" class="preset-btn" onclick="setPreset(37.7694,-122.4862)">SF Golden Gate</button>
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">Time available</label>
        <div class="slider-row">
          <input type="range" id="minutes" name="minutes" min="20" max="240" step="10" value="90" oninput="updateMins(this.value)">
          <span class="mins-badge" id="mins-val">90 min</span>
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">Activity</label>
        <div class="activity-grid">
          <label class="activity-card"><input type="radio" name="activity" value="walk" checked><div class="act-content"><span class="act-icon">🚶</span><span class="act-name">Walk</span></div></label>
          <label class="activity-card"><input type="radio" name="activity" value="run"><div class="act-content"><span class="act-icon">🏃</span><span class="act-name">Run</span></div></label>
          <label class="activity-card"><input type="radio" name="activity" value="birding"><div class="act-content"><span class="act-icon">🐦</span><span class="act-name">Birding</span></div></label>
          <label class="activity-card"><input type="radio" name="activity" value="garden"><div class="act-content"><span class="act-icon">🌱</span><span class="act-name">Garden</span></div></label>
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">Local Open Model</label>
        <input type="text" name="model" value="{default_model}">
        <div class="note">No model running? Fieldcard uses built-in sensory prompts automatically.</div>
      </div>
      <button type="submit" class="btn-submit" onclick="recordCard()">Generate Field Card</button>
    </form>
  </div>
  <div class="history-section">
    <div class="history-title">Recent field cards</div>
    <div class="history-list" id="history-list">
      <div class="history-empty">No cards yet. Generate your first one above!</div>
    </div>
  </div>
</div>
<script>
(function(){var c=document.getElementById('particles');for(var i=0;i<20;i++){var p=document.createElement('div');p.className='particle';p.style.left=Math.random()*100+'%';p.style.animationDuration=(8+Math.random()*12)+'s';p.style.animationDelay=Math.random()*8+'s';p.style.width=p.style.height=(2+Math.random()*3)+'px';c.appendChild(p);}})();
function updateMins(v){document.getElementById('mins-val').textContent=v+' min';}
function setPreset(la,lo){document.getElementById('lat').value=la;document.getElementById('lon').value=lo;fetchWeather(la,lo);}
function useLocation(){if(!navigator.geolocation){alert('Geolocation not supported.');return;}navigator.geolocation.getCurrentPosition(function(p){var la=p.coords.latitude.toFixed(4),lo=p.coords.longitude.toFixed(4);document.getElementById('lat').value=la;document.getElementById('lon').value=lo;fetchWeather(la,lo);},function(e){alert('Location error: '+e.message);},{timeout:7000});}
var WMO={0:'Clear',1:'Mostly clear',2:'Partly cloudy',3:'Overcast',45:'Fog',51:'Light drizzle',53:'Drizzle',61:'Light rain',63:'Rain',65:'Heavy rain',71:'Light snow',73:'Snow',80:'Showers',95:'Thunderstorm'};
var WMO_E={0:'\\u2600\\uFE0F',1:'\\uD83C\\uDF24',2:'\\u26C5',3:'\\u2601\\uFE0F',45:'\\uD83C\\uDF2B',51:'\\uD83C\\uDF26',53:'\\uD83C\\uDF27',61:'\\uD83C\\uDF26',63:'\\uD83C\\uDF27',65:'\\uD83C\\uDF27',71:'\\uD83C\\uDF28',73:'\\u2744\\uFE0F',80:'\\uD83C\\uDF26',95:'\\u26C8'};
function fetchWeather(lat,lon){fetch('https://api.open-meteo.com/v1/forecast?latitude='+lat+'&longitude='+lon+'&current=temperature_2m,wind_speed_10m,weather_code&temperature_unit=celsius').then(function(r){return r.json()}).then(function(d){var c=d.current||{};var code=c.weather_code||0;document.getElementById('wx-icon').textContent=WMO_E[code]||'\\uD83C\\uDF21';document.getElementById('wx-temp').textContent=(c.temperature_2m||'--')+'\\u00B0C';document.getElementById('wx-desc').textContent=(WMO[code]||'')+(c.wind_speed_10m?' \\u00B7 Wind '+Math.round(c.wind_speed_10m)+' km/h':'');document.getElementById('weather-pill').style.display='flex';}).catch(function(){});}
fetchWeather(40.7678,-73.9718);
var BADGES=[{id:'first',name:'First Steps',icon:'\\uD83C\\uDF31',need:1},{id:'s3',name:'3-Day Streak',icon:'\\uD83D\\uDD25',need:3},{id:'s7',name:'Week Explorer',icon:'\\u2B50',need:7},{id:'s14',name:'Fortnight',icon:'\\uD83C\\uDFC6',need:14},{id:'s30',name:'Monthly Master',icon:'\\uD83D\\uDC51',need:30},{id:'birder',name:'Bird Watcher',icon:'\\uD83D\\uDC26',need:1,type:'birding'},{id:'runner',name:'Trail Runner',icon:'\\uD83C\\uDFC3',need:1,type:'run'},{id:'gardener',name:'Green Thumb',icon:'\\uD83C\\uDF3B',need:1,type:'garden'}];
function getHistory(){try{return JSON.parse(localStorage.getItem('fc_history')||'[]');}catch(e){return[];}}
function getStreak(){var h=getHistory();if(!h.length)return 0;var streak=1;var dates=h.map(function(x){return x.date}).filter(function(v,i,a){return a.indexOf(v)===i}).sort().reverse();var today=new Date().toISOString().slice(0,10);var yd=new Date();yd.setDate(yd.getDate()-1);var yds=yd.toISOString().slice(0,10);if(dates[0]!==today&&dates[0]!==yds)return 0;for(var i=1;i<dates.length;i++){var d1=new Date(dates[i-1]),d2=new Date(dates[i]);if((d1-d2)===86400000)streak++;else break;}return streak;}
function recordCard(){var act=document.querySelector('input[name=activity]:checked');var e={date:new Date().toISOString().slice(0,10),time:new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),activity:act?act.value:'walk',lat:document.getElementById('lat').value,lon:document.getElementById('lon').value};var h=getHistory();h.unshift(e);if(h.length>50)h=h.slice(0,50);localStorage.setItem('fc_history',JSON.stringify(h));}
function renderStreak(){var s=getStreak();document.getElementById('streak-count').textContent=s;var f=document.getElementById('streak-fire');f.textContent=s>=7?'\\uD83D\\uDD25\\uD83D\\uDD25\\uD83D\\uDD25':s>=3?'\\uD83D\\uDD25\\uD83D\\uDD25':s>=1?'\\uD83D\\uDD25':'\\uD83D\\uDCA4';}
function renderBadges(){var h=getHistory(),s=getStreak(),acts={};h.forEach(function(x){acts[x.activity]=(acts[x.activity]||0)+1;});var total=h.length;var row=document.getElementById('badges-row');row.innerHTML='';BADGES.forEach(function(b){var earned=false;if(b.type)earned=(acts[b.type]||0)>=b.need;else if(b.id==='first')earned=total>=1;else earned=s>=b.need;var el=document.createElement('span');el.className='badge '+(earned?'earned':'locked');el.innerHTML='<span class="badge-icon">'+b.icon+'</span>'+b.name;el.title=earned?'Earned!':'Locked';row.appendChild(el);});}
function renderHistory(){var h=getHistory().slice(0,5);var list=document.getElementById('history-list');if(!h.length)return;var AE={walk:'\\uD83D\\uDEB6',run:'\\uD83C\\uDFC3',birding:'\\uD83D\\uDC26',garden:'\\uD83C\\uDF31'};list.innerHTML='';h.forEach(function(x){var el=document.createElement('div');el.className='history-item';el.innerHTML='<span class="history-emoji">'+(AE[x.activity]||'\\uD83D\\uDCCB')+'</span><div class="history-info"><span class="history-act">'+x.activity+'</span><span class="history-date"> \\u00B7 '+x.date+' at '+x.time+'</span></div>';list.appendChild(el);});}
renderStreak();renderBadges();renderHistory();
</script>
</body>
</html>
"""


def check_ollama_status() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if not host.startswith("http"):
        host = "http://" + host
    try:
        req = urllib.request.Request(host.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(req, timeout=1.0) as r:
            data = json.load(r)
            models = [m.get("name", "") for m in data.get("models", [])]
            if models:
                return f"Online ({len(models)} models available)"
            return "Online (no models pulled yet)"
    except Exception:
        return "Offline (built-in prompts active)"


class WebHandler(BaseHTTPRequestHandler):
    places_override: Optional[str] = None
    default_model: str = DEFAULT_MODEL

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            ollama_st = check_ollama_status()
            ollama_dot = "online" if "Online" in ollama_st else "offline"
            ollama_short = ollama_st.split("(")[0].strip()
            html_text = FORM_HTML\
                .replace("{ollama_dot}", ollama_dot)\
                .replace("{ollama_short}", ollama_short)\
                .replace("{ollama_status}", ollama_st)\
                .replace("{map_status}", "Online / Cached")\
                .replace("{default_model}", self.default_model)
            body = html_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, "Page Not Found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 65536:
            self.send_error(413, "Request body too large")
            return

        post_data = self.rfile.read(length).decode("utf-8")
        params = urllib.parse.parse_qs(post_data)

        try:
            lat = float(params.get("lat", [0])[0])
            lon = float(params.get("lon", [0])[0])
            minutes = int(params.get("minutes", [90])[0])
            activity = params.get("activity", ["walk"])[0]
            model = params.get("model", [self.default_model])[0].strip()

            if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
                raise ValueError("Latitude must be -90..90 and Longitude -180..180.")
            if not (10 <= minutes <= 600):
                raise ValueError("Minutes must be between 10 and 600.")
            if activity not in SPEEDS_KMH:
                raise ValueError(f"Invalid activity: {activity}")
            if not re.match(r"^[a-zA-Z0-9_\.:-]+$", model):
                raise ValueError("Invalid model tag format.")

            # Create an args namespace
            class ServerArgs:
                pass

            s_args = ServerArgs()
            s_args.lat = lat
            s_args.lon = lon
            s_args.minutes = minutes
            s_args.activity = activity
            s_args.model = model
            s_args.places = self.places_override
            s_args.no_llm = False
            s_args.no_net = False
            s_args.no_species = False
            s_args.date = None
            s_args.start = None
            s_args.tz = None
            s_args.frost_date = None
            s_args.group = False
            s_args.web = True

            card_html = build(s_args)
            body = card_html.encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except Exception as e:
            err_msg = f"<!doctype html><title>Error</title><body style='font-family:sans-serif;padding:30px;'><h2>Invalid Request</h2><p>{html.escape(str(e))}</p><a href='/'>← Back to form</a></body>"
            body = err_msg.encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        # Quiet logger during testing
        pass


def run_serve(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Start lightweight local web doorway for Fieldcard.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default 127.0.0.1)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Default Ollama model")
    parser.add_argument("--places", help="Optional places file override")
    args = parser.parse_args(argv)

    WebHandler.places_override = args.places
    WebHandler.default_model = args.model

    HTTPServer.allow_reuse_address = True
    server = HTTPServer((args.host, args.port), WebHandler)
    print(f"\n[Fieldcard] Local web doorway running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------- CLI entrypoint


def main(argv: Optional[List[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)

    # Dispatch subcommands
    if args_list and args_list[0] == "serve":
        return run_serve(args_list[1:])
    if args_list and args_list[0] == "log":
        return run_log(args_list[1:])

    ap = argparse.ArgumentParser(description="Print-first outdoor field cards, powered by open weights.")
    ap.add_argument("--lat", type=float, required=True, help="Your latitude")
    ap.add_argument("--lon", type=float, required=True, help="Your longitude")
    ap.add_argument("--minutes", type=int, default=90, help="Time available in minutes")
    ap.add_argument("--activity", choices=sorted(SPEEDS_KMH), default="walk", help="Activity type")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Any local Ollama model tag")
    ap.add_argument("--places", help="Path to JSON file of places instead of querying OpenStreetMap")
    ap.add_argument("--no-llm", action="store_true", help="Skip local LLM and use built-in sensory prompts")
    ap.add_argument("--no-net", action="store_true", help="Skip OpenStreetMap and iNaturalist lookups")
    ap.add_argument("--no-species", action="store_true", help="Skip iNaturalist species query")
    ap.add_argument("--frost-date", help="Date of frost (YYYY-MM-DD) for garden mode planting advice")
    ap.add_argument("--group", action="store_true", help="Run-club mode with pace routes and QR code")
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    ap.add_argument("--start", help="HH:MM local start (default now)")
    ap.add_argument("--tz", type=float, help="UTC offset hours (default system)")
    ap.add_argument("--out", default="fieldcard.html", help="Output file path (default fieldcard.html)")

    args = ap.parse_args(args_list)
    if args.minutes < 10:
        ap.error("--minutes must be at least 10")

    rendered = build(args)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"Wrote {args.out}. Open it, print it (A5), then put the screen away.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
