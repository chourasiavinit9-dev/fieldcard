# Fieldcard

**The screen is the shortest part of the experience, so Fieldcard ends in paper.**

Tell it where you are and how long you have. A local open-weight model writes a one-page A5 field card:
a mission, a map of real nearby places, sunset and turn-around times, and a 3×3 "notice nine things" bingo.
You print it, put your phone in your pocket, and go. Nothing on the trail needs signal, battery or a login.

## Why open models are the point

| Piece | Open component | What it does |
|---|---|---|
| Text | Any open-weight model via [Ollama](https://ollama.com) (default `llama3.2:3b`; try `gemma3:4b`, `qwen2.5:3b`) | Writes the title, mission and seasonal "notice" prompts, grounded in your real places |
| Places | [OpenStreetMap](https://www.openstreetmap.org) via Overpass | Parks, water, viewpoints, peaks within your real reach |
| Sun / season | NOAA equations, in plain code | Sunset, latest safe start, hemisphere-aware season |

Runs fully local after the first map lookup (results are cached). No API keys, no accounts, no telemetry.

**Design rule: the LLM writes the poetry, the code does the safety math.** Daylight warnings, turn-around
times and reach are computed deterministically. Model output is length-clipped, validated field by field,
and HTML-escaped. If Ollama or the network is down, Fieldcard still prints a good card from built-in prompts.

## Run it

```bash
ollama pull llama3.2:3b          # once; any model tag works
python fieldcard.py --lat 40.7678 --lon -73.9718 --minutes 90 --activity walk
# then open fieldcard.html and print at A5 (or "fit to page")
```

No dependencies beyond Python 3.9+. Useful flags:

- `--activity walk|run|birding|garden` changes pace, reach and mission
- `--model gemma3:4b` swaps the open model
- `--places file.json` uses your own places; `--no-net` skips OpenStreetMap; `--no-llm` skips the model
- `--start 16:30 --date 2026-10-10` plans ahead

## Test

```bash
python -m unittest discover -s tests -v
```

Includes a mock Ollama server so the model path is tested without downloading weights.

## Honest limits

- Map distances are straight-line with a 1.3x detour factor. The card says so; it is not a navigator.
- Season prompts are generic, not species-level. Pair with a local bird-ID model for the trail.
- Tropical latitudes use one "tropical" bank rather than monsoon/dry splits.

## Next

- Whisper/BirdNET-style on-device call ID that logs your bingo from audio
- Frost-date garden mode that tells you what to sow this week
- Run-club mode: one shared card for a group, QR to the route
