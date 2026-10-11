#!/usr/bin/env python3
"""Fieldcard: turn "I have 90 minutes" into a one-page printed field card.

Open pieces that make it work:
  * an open-weight model served locally by Ollama (default: llama3.2:3b) writes the
    mission + "notice" bingo, so it works with no cloud and no signal once you've printed
  * OpenStreetMap (Overpass) supplies real nearby parks, water, viewpoints, peaks
  * sun/season math is plain code (NOAA equations), never left to the model

Design rule: the LLM writes the poetry, the code does the safety math.
If Ollama or the network is missing, Fieldcard still produces a good card.

Usage:
  python fieldcard.py --lat 40.7678 --lon -73.9718 --minutes 90 --activity walk
  python fieldcard.py --lat .. --lon .. --places examples/demo_places_nyc.json --no-llm
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import random
import sys
import urllib.parse
import urllib.request

SPEEDS_KMH = {"walk": 4.5, "run": 9.0, "birding": 2.5, "garden": 0.0}
DETOUR = 1.3  # straight-line distance -> real path distance fudge
OVERPASS = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
DEFAULT_MODEL = os.environ.get("FIELDCARD_MODEL", "llama3.2:3b")
ACCENT = {"spring": "#3f7d3a", "summer": "#b8860b", "autumn": "#c4551d",
          "winter": "#3b6a8f", "tropical": "#1f8a70"}

# ---------------------------------------------------------------- geometry

def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg):
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int((deg + 22.5) // 45) % 8]


# ---------------------------------------------------------------- sun + season

def sun_events(day, lat, lon, tz_hours):
    """NOAA solar equations. Returns naive local datetimes (or None at polar extremes)."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lr = math.radians(lat)

    def event(zenith, rising):
        c = (math.cos(math.radians(zenith)) - math.sin(lr) * math.sin(decl)) / (
            math.cos(lr) * math.cos(decl))
        if not -1 <= c <= 1:
            return None
        h = math.degrees(math.acos(c))
        minutes = 720 - 4 * (lon + (h if rising else -h)) - eqt + tz_hours * 60
        return dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=minutes)

    return {"sunrise": event(90.833, True), "sunset": event(90.833, False),
            "dusk": event(96.0, False)}


def season_for(lat, month):
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


def fmt_t(t):
    if t is None:
        return "--"
    h = t.hour
    return f"{h % 12 or 12}:{t.minute:02d} {'am' if h < 12 else 'pm'}"


# ---------------------------------------------------------------- places (OpenStreetMap)

def reach_km(activity, minutes):
    """How far (straight line) you can go and still get back within the time."""
    return SPEEDS_KMH[activity] * (minutes / 60.0) / 2.0 / DETOUR


def _kind(tags):
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    nat = tags.get("natural")
    if nat in ("peak", "water", "wood"):
        return {"water": "water", "peak": "peak", "wood": "woods"}[nat]
    if "waterway" in tags:
        return "stream"
    leis = tags.get("leisure", "")
    return {"nature_reserve": "reserve"}.get(leis, leis or "place")


def parse_overpass(data, lat, lon):
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


def fetch_places(lat, lon, radius_m, cache_dir=None, timeout=25):
    key = hashlib.sha1(f"{lat:.3f},{lon:.3f},{int(radius_m)}".encode()).hexdigest()[:12]
    cache_dir = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "fieldcard")
    path = os.path.join(cache_dir, key + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    q = (f'[out:json][timeout:20];('
         f'nwr["leisure"~"park|nature_reserve|garden"]["name"](around:{radius_m},{lat},{lon});'
         f'nwr["tourism"="viewpoint"](around:{radius_m},{lat},{lon});'
         f'nwr["natural"~"peak|water|wood"]["name"](around:{radius_m},{lat},{lon});'
         f'nwr["waterway"~"river|stream"]["name"](around:{radius_m},{lat},{lon});'
         f');out center 60;')
    req = urllib.request.Request(
        OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
        headers={"User-Agent": "fieldcard/1.0 (hackathon project)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        places = parse_overpass(json.load(r), lat, lon)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump(places, f)
    return places


def pick_places(raw, lat, lon, activity, minutes, limit=5):
    """Keep places you can reach and return from in time; prefer variety of kinds."""
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
    for p in cands:  # first pass: one of each kind
        if p["kind"] not in kinds and len(chosen) < limit:
            chosen.append(p)
            kinds.add(p["kind"])
    for p in cands:  # second pass: fill up
        if p not in chosen and len(chosen) < limit:
            chosen.append(p)
    return sorted(chosen, key=lambda p: p["dist"])


# ---------------------------------------------------------------- copy: fallback + local LLM

SEASONAL = {
    "autumn": ["A leaf showing three colours at once", "Something being stored for winter",
               "Fungus growing on a log", "A spider web catching light",
               "The oldest tree you can find", "Seeds that travel by wind or fur"],
    "spring": ["The first flower in any colour", "A bird carrying nesting material",
               "New leaves still folded up", "Mud with a footprint in it",
               "Something buzzing at a blossom", "Moss that looks freshly watered"],
    "summer": ["The shadiest spot within sight", "An insect you can't name",
               "Something ripening", "Where the breeze is coolest",
               "A flower being visited by a bee", "Water moving, however small"],
    "winter": ["A bare branch's shape against the sky", "Tracks in mud, frost or snow",
               "A bud already waiting for spring", "Your breath in the air",
               "Evergreen versus bare: count both", "Frost, ice or dew on anything"],
    "tropical": ["Something that flowers right now", "A bird calling from the canopy",
                 "An ant trail and where it leads", "Rain, or the smell just before it",
                 "A tree with something living on it", "The tallest tree within sight"],
}
UNIVERSAL = ["A sound with no engine in it", "Something older than you are",
             "Three shades of green (or brown)", "A smell worth stopping for",
             "Something a child would pick up", "Your own shadow, stretched or short"]

MISSIONS = {
    "walk": "Walk at an easy pace to your farthest stop, then loop home by a different path if you can.",
    "run": "Keep it conversational. Run out to your farthest stop, walk the last five minutes home.",
    "birding": "Stand still for three minutes at every stop. Count what you hear before what you see.",
    "garden": "Go and stand in your garden or balcony for a while. Touch the soil, check each plant once, fix one small thing.",
}


def fallback_copy(facts, rng):
    items = rng.sample(SEASONAL[facts["season"]], 6) + rng.sample(UNIVERSAL, 3)
    rng.shuffle(items)
    return {"title": f"{facts['minutes']} minutes outside",
            "mission": MISSIONS[facts["activity"]],
            "notice": items,
            "send_off": "Phone in pocket. Eyes up.",
            "source": "built-in prompts"}


def _clip(s, words):
    w = str(s).replace("\n", " ").split()
    return " ".join(w[:words])


def parse_llm_json(text, fallback):
    """Validate model output; anything malformed falls back field by field."""
    try:
        d = json.loads(text)
        assert isinstance(d, dict)
    except Exception:
        return None
    out = dict(fallback)
    if isinstance(d.get("title"), str) and d["title"].strip():
        out["title"] = _clip(d["title"], 7)
    if isinstance(d.get("mission"), str) and d["mission"].strip():
        out["mission"] = _clip(d["mission"], 45)
    if isinstance(d.get("send_off"), str) and d["send_off"].strip():
        out["send_off"] = _clip(d["send_off"], 14)
    notice = [_clip(x, 9) for x in d.get("notice", []) if isinstance(x, str) and x.strip()]
    if len(notice) >= 5:
        notice += fallback["notice"]  # pad if the model gave fewer than 9
        out["notice"] = notice[:9]
    out["source"] = "local model"
    return out


def llm_copy(facts, places, fallback, model=DEFAULT_MODEL, host=None, timeout=90):
    host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if not host.startswith("http"):
        host = "http://" + host
    names = "; ".join(f"{p['name']} ({p['kind']}, {p['dist']:.1f} km {p['dir']})"
                      for p in places) or "none known"
    prompt = (
        "You write the text for a one-page PRINTED field card that sends someone outdoors.\n"
        f"Facts (never contradict): season={facts['season']}; activity={facts['activity']}; "
        f"time available={facts['minutes']} minutes.\n"
        f"Nearby places (use only these names, never invent places): {names}.\n"
        "Return ONLY JSON with keys: title (max 6 words), mission (max 40 words, concrete, "
        "second person, may mention one nearby place), notice (exactly 9 items, each max 8 words, "
        "things to find or notice outdoors this season, nothing involving phones or apps), "
        "send_off (max 14 words). Warm and brief. No emojis.")
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json",
                       "options": {"temperature": 0.8}}).encode()
    req = urllib.request.Request(host.rstrip("/") + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_llm_json(json.load(r).get("response", ""), fallback)
    except Exception:
        return None


# ---------------------------------------------------------------- rendering

CSS = """
@page{size:A5;margin:9mm}
*{box-sizing:border-box}
body{margin:0;background:#e9e4d6;font-family:Georgia,'Times New Roman',serif;color:#1d2b22}
.card{width:148mm;min-height:210mm;margin:8mm auto;background:#f8f4e8;padding:9mm;
 border:1px solid #cfc7b0;position:relative}
@media print{body{background:none}.card{margin:0;border:0;width:auto;min-height:0}}
.kicker{font:700 8pt/1 Helvetica,Arial,sans-serif;letter-spacing:.18em;text-transform:uppercase;color:var(--a)}
h1{font-size:25pt;line-height:1.05;margin:3mm 0 4mm}
.stats{display:flex;gap:2mm;margin-bottom:4mm}
.stat{flex:1;border:1.2px solid #1d2b22;padding:2mm 2.5mm}
.stat b{display:block;font:700 7pt Helvetica,Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:var(--a)}
.stat span{font-size:13pt}
.warn{background:#fbe3d3;border-left:3mm solid var(--a);padding:2mm 3mm;margin-bottom:4mm;font-size:10pt}
.mission{font-size:12.5pt;line-height:1.35;margin:0 0 4mm}
.mid{display:flex;gap:4mm;margin-bottom:4mm}
.mid svg{width:62mm;height:62mm;flex:none}
ol.legend{margin:0;padding-left:5mm;font-size:9.5pt;line-height:1.35}
ol.legend small{display:block;color:#5b6b60}
.rule{border:1.2px dashed #1d2b22;padding:3mm;font-size:10.5pt;flex:1}
h2{font:700 8pt Helvetica,Arial,sans-serif;letter-spacing:.16em;text-transform:uppercase;margin:0 0 2mm;color:var(--a)}
.bingo{display:flex;flex-wrap:wrap;gap:1.5mm}
.cell{width:calc(33.333% - 1mm);border:1.2px solid #1d2b22;padding:2mm;min-height:17mm;font-size:9.5pt;line-height:1.2;position:relative}
.cell i{position:absolute;right:1.5mm;bottom:1.5mm;width:4mm;height:4mm;border:1.2px solid #1d2b22}
.foot{margin-top:5mm;border-top:1.2px solid #1d2b22;padding-top:2.5mm;font-size:10pt;display:flex;justify-content:space-between;gap:4mm}
.tiny{font:7.5pt Helvetica,Arial,sans-serif;color:#5b6b60;margin-top:2mm}
"""


def map_svg(places, rk, accent):
    S, c, R = 360, 180, 150
    p = [f'<svg viewBox="0 0 {S} {S}" xmlns="http://www.w3.org/2000/svg" '
         f'font-family="Helvetica,Arial,sans-serif">',
         f'<rect width="{S}" height="{S}" fill="none" stroke="#1d2b22" stroke-width="2"/>',
         f'<circle cx="{c}" cy="{c}" r="{R}" fill="none" stroke="#1d2b22" stroke-width="1.5"/>',
         f'<circle cx="{c}" cy="{c}" r="{R / 2}" fill="none" stroke="#1d2b22" stroke-dasharray="4 4"/>',
         f'<text x="{c}" y="22" text-anchor="middle" font-size="16" font-weight="700">N</text>',
         f'<text x="{c + R - 4}" y="{c - 6}" text-anchor="end" font-size="11">{rk:.1f} km</text>',
         f'<text x="{c + R / 2 - 4}" y="{c - 6}" text-anchor="end" font-size="11">{rk / 2:.1f}</text>',
         f'<rect x="{c - 6}" y="{c - 6}" width="12" height="12" fill="#1d2b22"/>',
         f'<text x="{c + 10}" y="{c + 20}" font-size="11" font-weight="700">START</text>']
    for i, pl in enumerate(places, 1):
        r = min(pl["dist"] / rk, 1.0) * R
        a = math.radians(pl["bearing"])
        x, y = c + r * math.sin(a), c - r * math.cos(a)
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="13" fill="{accent}"/>')
        p.append(f'<text x="{x:.1f}" y="{y + 5:.1f}" text-anchor="middle" font-size="15" '
                 f'font-weight="700" fill="#fff">{i}</text>')
    p.append("</svg>")
    return "".join(p)


def render_card(facts, places, copy, sun, start, end):
    e, a = html.escape, ACCENT[facts["season"]]
    mins, act = facts["minutes"], facts["activity"]
    turn = start + dt.timedelta(minutes=mins / 2)
    warn = ""
    if sun["sunset"] and end > sun["sunset"]:
        latest = sun["sunset"] - dt.timedelta(minutes=mins + 10)
        warn = (f'<div class="warn"><b>Daylight check:</b> this ends after sunset '
                f'({e(fmt_t(sun["sunset"]))}). Start by {e(fmt_t(latest))} to finish in the light, '
                f'or carry a torch.</div>')
    if places:
        legend = "".join(
            f'<li>{e(p["name"])}<small>{e(p["kind"])} · ~{p["dist"]:.1f} km {e(p["dir"])} · '
            f'≈{p["mins"]:.0f} min each way</small></li>' for p in places)
        mid = (f'<div class="mid">{map_svg(places, max(p["dist"] for p in places) * 1.15, a)}'
               f'<ol class="legend">{legend}</ol></div>')
    elif act == "garden":
        mid = ""
    else:
        mid = (f'<div class="mid"><div class="rule"><h2>No map data? Use the rule</h2>'
               f'Head out with the sun behind you. Turn around at <b>{e(fmt_t(turn))}</b> '
               f'wherever you are, no matter how good the view.</div></div>')
    cells = "".join(f'<div class="cell">{e(n)}<i></i></div>' for n in copy["notice"][:9])
    stats = (f'<div class="stat"><b>Out for</b><span>{mins} min</span></div>'
             + ('' if act == "garden" else
                f'<div class="stat"><b>Turn around</b><span>{e(fmt_t(turn))}</span></div>')
             + f'<div class="stat"><b>Sunset</b><span>{e(fmt_t(sun["sunset"]))}</span></div>')
    return (
        f'<!doctype html><html lang="en"><meta charset="utf-8"><title>Fieldcard</title>'
        f'<style>{CSS}</style><body style="--a:{a}"><div class="card">'
        f'<div class="kicker">Field card · {e(facts["date"])} · {e(act)} · {e(facts["season"])}</div>'
        f'<h1>{e(copy["title"])}</h1><div class="stats">{stats}</div>{warn}'
        f'<p class="mission">{e(copy["mission"])}</p>{mid}'
        f'<h2>Notice nine things</h2><div class="bingo">{cells}</div>'
        f'<div class="foot"><div><b>{e(copy["send_off"])}</b><br>Back by {e(fmt_t(end))}. '
        f'Tell someone where you are going.</div></div>'
        f'<div class="tiny">Text: {e(copy["source"])} · Places: OpenStreetMap contributors · '
        f'Distances are straight-line estimates; trust the ground, not the card.</div>'
        f'</div></body></html>')


# ---------------------------------------------------------------- main

def build(args):
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    tz = args.tz if args.tz is not None else (
        dt.datetime.now().astimezone().utcoffset().total_seconds() / 3600)
    if args.start:
        hh, mm = map(int, args.start.split(":"))
        start = dt.datetime.combine(day, dt.time(hh, mm))
    else:
        start = dt.datetime.combine(day, dt.datetime.now().time()).replace(second=0, microsecond=0)
    end = start + dt.timedelta(minutes=args.minutes)
    sun = sun_events(day, args.lat, args.lon, tz)
    facts = {"date": day.strftime("%a %d %b %Y"), "season": season_for(args.lat, day.month),
             "activity": args.activity, "minutes": args.minutes}

    places = []
    if args.activity != "garden":
        raw = []
        if args.places:
            with open(args.places) as f:
                raw = json.load(f)
        elif not args.no_net:
            try:
                raw = fetch_places(args.lat, args.lon,
                                   int(reach_km(args.activity, args.minutes) * 1000))
            except Exception as ex:
                print(f"[fieldcard] no map data ({ex.__class__.__name__}); using offline rule",
                      file=sys.stderr)
        places = pick_places(raw, args.lat, args.lon, args.activity, args.minutes)

    rng = random.Random(f"{day}{args.lat:.2f}{args.lon:.2f}{args.activity}")
    copy = fallback_copy(facts, rng)
    if not args.no_llm:
        got = llm_copy(facts, places, copy, model=args.model)
        if got:
            copy = got
        else:
            print("[fieldcard] local model unreachable; using built-in prompts "
                  "(is `ollama serve` running?)", file=sys.stderr)
    return render_card(facts, places, copy, sun, start, end)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Print-first outdoor field cards, powered by open weights.")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--minutes", type=int, default=90)
    ap.add_argument("--activity", choices=sorted(SPEEDS_KMH), default="walk")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="any Ollama model tag")
    ap.add_argument("--places", help="JSON file of places instead of querying OpenStreetMap")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-net", action="store_true", help="skip the OpenStreetMap lookup")
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    ap.add_argument("--start", help="HH:MM local start (default now)")
    ap.add_argument("--tz", type=float, help="UTC offset hours (default system)")
    ap.add_argument("--out", default="fieldcard.html")
    args = ap.parse_args(argv)
    if args.minutes < 10:
        ap.error("--minutes must be at least 10")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build(args))
    print(f"Wrote {args.out}. Open it, print it (A5), then put the screen away.")


if __name__ == "__main__":
    main()
