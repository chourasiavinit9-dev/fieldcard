#!/usr/bin/env python3
"""listen.py: On-device bird call classifier for Fieldcard's birding mode.

Runs the open-source BirdNET model fully on-device on a recorded .wav file
to detect species and tick matching items on your field card.
Works completely offline with zero cell signal and zero telemetry.

If BirdNET (birdnetlib) is not installed, prints clean installation steps
and exits cleanly without crashing.
"""

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Tuple


def check_birdnet_installed() -> bool:
    try:
        import birdnetlib  # noqa: F401
        return True
    except ImportError:
        return False


def print_install_guide():
    print("""
[Fieldcard Bird-Call Mode]
BirdNET is an open-source acoustic bird monitoring model developed by
the Cornell Lab of Ornithology and Chemnitz University of Technology.

To run BirdNET fully offline on your device, install birdnetlib:

    pip install birdnetlib

Optional system dependencies (for audio decoding):
    macOS:  brew install ffmpeg
    Linux:  sudo apt install ffmpeg

Once installed, record a short clip outside and run:
    python listen.py --audio sample.wav --card fieldcard.html
""")


def analyze_audio(wav_path: str, lat: Optional[float] = None, lon: Optional[float] = None,
                  min_conf: float = 0.25) -> List[Dict[str, any]]:
    from birdnetlib import Recording
    from birdnetlib.analyzer import Analyzer
    from datetime import datetime

    analyzer = Analyzer()
    recording = Recording(
        analyzer,
        wav_path,
        lat=lat if lat is not None else 0.0,
        lon=lon if lon is not None else 0.0,
        date=datetime.now(),
        min_conf=min_conf,
    )
    recording.analyze()
    detections = []
    seen = set()
    for det in recording.detections:
        common_name = det.get("common_name", "Unknown bird")
        confidence = float(det.get("confidence", 0.0))
        if common_name not in seen:
            seen.add(common_name)
            detections.append({
                "common_name": common_name,
                "scientific_name": det.get("scientific_name", ""),
                "confidence": confidence,
            })
    detections.sort(key=lambda x: x["confidence"], reverse=True)
    return detections


def tick_card(card_path: str, detected_names: List[str]) -> Tuple[int, str]:
    if not os.path.exists(card_path):
        return 0, f"Card not found at {card_path}"
    with open(card_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    ticked = 0
    lower_names = [n.lower() for n in detected_names]

    # Pattern matches bingo cell: <div class="cell">(content)<i></i></div>
    def replacer(match):
        nonlocal ticked
        cell_text = match.group(1)
        for name in lower_names:
            if name in cell_text.lower():
                ticked += 1
                return f'<div class="cell" style="background:#e8f4ec;font-weight:bold;">{cell_text}<i style="background:#3f7d3a;color:#fff;text-align:center;line-height:4mm;font-size:7pt;font-style:normal;">✓</i></div>'
        return match.group(0)

    updated_html = re.sub(r'<div class="cell">([^<]+)<i></i></div>', replacer, html_content)
    if ticked > 0:
        with open(card_path, "w", encoding="utf-8") as f:
            f.write(updated_html)
    return ticked, f"Ticked {ticked} items on {card_path}"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Classify bird calls on-device using BirdNET and tick off your Fieldcard."
    )
    parser.add_argument("--audio", help="Path to .wav audio recording")
    parser.add_argument("--card", default="fieldcard.html", help="Path to fieldcard.html to tick off")
    parser.add_argument("--lat", type=float, help="Latitude where recording was made")
    parser.add_argument("--lon", type=float, help="Longitude where recording was made")
    parser.add_argument("--min-conf", type=float, default=0.25, help="Minimum confidence threshold (0.0 to 1.0)")
    parser.add_argument("--demo", action="store_true", help="Simulate a detection for testing without audio")
    parser.add_argument("--demo-species", default="Rock Pigeon,Song Sparrow", help="Comma-separated demo species")

    args = parser.parse_args(argv)

    if args.demo:
        print("[listen.py] Demo mode: simulating on-device detections")
        detected = [{"common_name": s.strip(), "confidence": 0.88} for s in args.demo_species.split(",")]
    else:
        if not check_birdnet_installed():
            print_install_guide()
            return 0

        if not args.audio:
            parser.error("--audio <file.wav> is required (or use --demo for testing)")

        if not os.path.exists(args.audio):
            print(f"Error: Audio file not found: {args.audio}", file=sys.stderr)
            return 1

        print(f"[listen.py] Analyzing {args.audio} with local BirdNET model...")
        detected = analyze_audio(args.audio, lat=args.lat, lon=args.lon, min_conf=args.min_conf)

    if not detected:
        print("No bird calls detected above confidence threshold.")
        return 0

    print(f"\nIdentified {len(detected)} species:")
    for d in detected:
        pct = int(d["confidence"] * 100)
        print(f"  • {d['common_name']} ({pct}% confidence)")

    if os.path.exists(args.card):
        names = [d["common_name"] for d in detected]
        ticked, msg = tick_card(args.card, names)
        print(f"\nCard status: {msg}")
    else:
        print(f"\n(No card found at {args.card} to update)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
