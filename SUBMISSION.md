# Fieldcard: Submission Write-up

**Challenge**: Hacktoberfest Open-Source AI Challenge: Week 1 (Touch Grass)  
**Tag**: `#hf26challenge`

### Inspiration
We reach for our phones to decide whether to go outside, then never leave the screen. We open an app to check a trail, only to be ambushed by notifications, battery warnings, and feeds. We wanted the opposite: an experience where the screen is the shortest part, and everything ends on a physical sheet of paper.

### What it does
Fieldcard turns *"I have 90 minutes"* into a one-page printed A5 field card. 
You tell it your location, your available minutes, and your activity (`walk`, `run`, `birding`, `garden`). It produces a tactile card with:
1. A seasonal mission and 3×3 "notice nine things" sensory bingo written by an open-weight model.
2. A radial compass map of real nearby parks, viewpoints, and waterways from OpenStreetMap.
3. Real flora and fauna species observed nearby this month via iNaturalist open data.
4. Deterministic daylight math (NOAA equations) giving exact turn-around and sunset times.

You print the card, put your phone in your pocket, and head out. Nothing on the trail needs signal, battery, or a login. When you return, `python fieldcard.py log` records what you found into your personal lifelong seasonal journal.

### How we built it
* **Open-weight AI**: Local inference via Ollama (`llama3.2:3b`, `gemma2:2b`, `qwen2.5:3b`) generates sensory copy grounded in real landmark names.
* **Open data**: OpenStreetMap (Overpass API) supplies terrain geometry; iNaturalist supplies monthly local wildlife records.
* **Deterministic safety**: NOAA solar equations compute sunrise, sunset, and safe turn-around times in plain standard-library Python. The LLM never touches safety math.
* **Lightweight core**: Zero external Python dependencies in the core CLI and local web doorway (`python fieldcard.py serve`). Calibrated print CSS guarantees single-page A5 fit.

### Challenges we overcame
* **Taming open-weight models**: Small models love generic clichés (*"embrace the tapestry of nature"*). We engineered strict few-shot constraints and a banned-phrase filter to enforce tactile, sensory prompts.
* **Public Overpass rate limits**: Global Overpass endpoints frequently returned HTTP 504 timeouts on heavy geometric relations. We refactored to node/way queries with mirror failovers and local caching.
* **A5 print fidelity**: Ensuring zero accidental multi-page spillovers across 0, 3, and 5 waypoints required millimeter-level print stylesheet calibration.

### What we learned
Open models excel when you treat them as poets rather than calculators. Giving the model narrow creative freedom while anchoring safety in deterministic code produces reliable, delightful tools that respect human attention.

### What's next
* On-device acoustic call identification using open-source BirdNET (`listen.py`).
* Frost-date garden planner with localized crop calendars.
* Shared run-club cards with pace groups and inline SVG QR codes.
