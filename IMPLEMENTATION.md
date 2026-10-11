# Fieldcard Implementation Plan & Architecture Specification

## 1. Project Overview & Hackathon Theme
**Theme**: MLH Open-Source AI — build with open-weight models or open-source AI that gets people off the screen and into the physical world.
**Core Principle**: *"The screen is the shortest part of the experience, so Fieldcard ends in paper."*

### Judging Criteria Priorities (in order of weight)
1. **Writing Quality** *(Weighted most heavily)*: Clear, concrete, sensory, devoid of hype/clichés, immediately compelling in 30 seconds.
2. **Relevance to Theme**: "The screen is the shortest part of the experience" (<60 seconds on screen, zero signal needed on the trail).
3. **Creativity**: Tangible rituals, post-walk journal loop (`fieldcard.py log`), sensory bingo.
4. **Technical Execution**: Deterministic safety math, offline resilience, live Ollama/Overpass verification, single-page A5 print guarantee, 100% clean tests (`python -W error::ResourceWarning`).
5. **Partner Technology (Optional)**: Open-weight partner models (such as Google Gemma via Ollama).

---

## 2. Inviolable Design Rules
1. **The LLM writes poetry; code does the safety math**:
   - Daylight warnings, turn-around times, and reach are strictly computed in code (NOAA solar equations). The model NEVER produces times or distances.
2. **Model output is untrusted**:
   - Strict length clipping, JSON schema validation, HTML entity escaping, and graceful field-by-field fallback.
3. **Graceful offline degradation**:
   - Runs cleanly with no Ollama and no network connectivity, falling back to curated sensory prompts and deterministic dead-reckoning rules.
4. **Core stdlib-only**:
   - Core CLI and server remain 100% Python standard library (`urllib`, `json`, `math`, `argparse`, `http.server`, etc.). No CDN, no npm, no build steps, no external fonts.
5. **Paper & outdoors focus**:
   - The finished product is a physical sheet of paper, not a digital app.

---

## 3. Work Breakdown by Priority

### Priority 1: Writing Quality & Voice (Highest Weight)
- [ ] **README Overhaul**:
  - One-sentence hook.
  - 3-sentence story: The problem (reaching for phones to plan, getting trapped by feeds) and the fix (screen ends in paper).
  - Real card screenshot.
  - "Try it in 30 seconds" quickstart via `python fieldcard.py serve`.
  - How it works in plain language.
  - Why open weights are essential (private, offline, zero API keys, swappable).
  - Honest limits and what's next.
  - Crisp sentences, concrete nouns, zero hype words, zero filler, zero emojis.
- [ ] **Voice of the Card & Built-in Prompts**:
  - Line-by-line rewrite of `SEASONAL`, `UNIVERSAL`, `MISSIONS`, and send-offs.
  - Sensory standard: "Find something that was alive this summer and isn't now", not "Notice nature".
  - Cut weak or vague prompts across all seasons.
- [ ] **Prompt Engineering & Evaluations**:
  - Few-shot examples in system prompt.
  - Banned-phrase list: "embrace", "journey", "tapestry", "immerse", "unplug", "nature's beauty".
  - Self-check constraint in prompt.
  - Run model evaluations (10 runs per season) documented in `docs/PROMPT_NOTES.md` with before & after comparisons.
- [ ] **Documentation**:
  - `DEMO.md`: 90-second script (screen visible for only the first 15 seconds).
  - `docs/HOW_IT_WORKS.md`: Single-page architectural walkthrough with ASCII/Mermaid diagram.
  - Clear `--help` text and actionable error diagnostics.
  - `SUBMISSION.md`: Devpost-style story under 500 words (inspiration, implementation, challenges, learnings, future).

### Priority 2: Relevance to Theme & Open Data Integration
- [ ] **Measurable "Screen Minimization"**:
  - Document and demonstrate <30s UI / <60s CLI generation time.
- [ ] **Load-Bearing Open Pieces**:
  - Open-weight LLMs via Ollama.
  - OpenStreetMap via Overpass API.
  - iNaturalist open API: Monthly observations near lat/lon (`api.inaturalist.org/v1/observations/species_counts`), cached locally, non-blocking (3s timeout) to supply real "Look for: [species]" prompts.
  - Document explicitly what happens when each piece is removed or offline.

### Priority 3: Creativity — The "Found It" Post-Walk Loop
- [ ] **`fieldcard.py log`**:
  - CLI command to record what was found after the walk.
  - Quick, photo-free checklist or prompt.
  - Appends a concise timestamped field note to a local journal (`fieldnotes.md`).
- [ ] *(Secondary)* Garden & Run-Club modes if core items are perfected.

### Priority 4: Local Web UI (`python fieldcard.py serve`)
- [ ] **Pure stdlib `http.server`**:
  - Runs on `127.0.0.1:8000` with `--port` and `--host` flags.
  - Zero external assets, no build step, no CDN, works 100% offline.
- [ ] **Form Page & UX**:
  - Intro: *"Tell us where and for how long. Leave with a page, not an app."*
  - Place: "Use my location" button (browser GPS) + manual lat/lon inputs + one-click presets (e.g. Central Park NYC, Hyde Park London, Royal Botanic Gardens Melbourne).
  - Minutes: 30 to 240 slider/select (default 90).
  - Activity: Large tap targets for `walk`, `run`, `birding`, `garden`.
  - Start time: Default to local time.
  - Model: Text field with fallback explanation note.
  - Status indicators: Ollama (running / not found via quick `/api/tags` probe) and Map data (online / cached / offline).
- [ ] **Card Page & Actions**:
  - Calls `build()` on POST and renders the card.
  - Prominent "Print Card" button (`window.print()`) and "Make another" link.
  - No auto-print.
- [ ] **Security & Validation**:
  - Strict input validation: lat (-90..90), lon (-180..180), minutes (10..600), activity whitelist, model name regex.
  - Request body size limits. HTML-escaping throughout. Friendly error messages without stack traces.
- [ ] **Styling**:
  - Responsive single-column layout (tested at 390px phone and desktop).
  - Prefers-color-scheme dark mode for form only (card remains authentic paper color).

### Priority 5: Technical Execution & Validation
- [ ] **Live Ollama Verification**:
  - Install/serve Ollama, pull `llama3.2:3b` and partner model (e.g. `gemma2:2b` / `gemma3:4b`).
  - Document output quality and speed comparison.
- [ ] **Live Overpass Validation**:
  - Query 3 live locations: City park (NYC), rural area (sparse trail with radius expansion), Southern Hemisphere (Melbourne/Sydney).
  - Resilient handling of timeouts, HTTP 429/504, and empty responses.
- [ ] **A5 Single-Page Print Proof**:
  - Headless Chrome rendering test across 0, 3, and 5 places, and with max-clipped LLM copy.
  - Visual layout verification using rendered PNG/PDF.
- [ ] **Zero Warning Test Suite**:
  - Fix test socket leak so `python -W error::ResourceWarning -m unittest discover -s tests` runs 100% green without warnings.
  - Unit tests for web server (GET 200, valid POST 200 with place name, invalid POST 400, offline Ollama).
  - Unit tests for all failure paths (network timeout, corrupt JSON, offline fallback).
- [ ] **Repository Setup**:
  - Git repository initialization, `.gitignore`, MIT `LICENSE`, clean incremental commits.
  - Pre-generated sample cards (HTML and PDF) in `examples/` for urban walk, birding, and autumn season.

### Priority 6: Partner Technology
- [ ] Confirm specific hackathon partner technology with user.
- [ ] Support open-weight partner model (e.g. Google Gemma via Ollama) with honest evaluation notes.
