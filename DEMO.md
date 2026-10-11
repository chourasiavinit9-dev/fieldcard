# Fieldcard: 90-Second Demo Script

**Theme**: MLH Open-Source AI (Touch Grass)  
**Rule**: The screen is the shortest part of the experience. The screen is visible for only the first 15 seconds.

---

### [0:00 – 0:15] The 15-Second Screen Window
* **Visual**: A clean browser window open to `http://localhost:8000`.
* **Action**:
  1. Click **NYC Central Park** (or "Use my location").
  2. Slide time budget to **60 minutes**.
  3. Select **Walk**.
  4. Click **Generate Field Card**.
  5. The card instantly renders with real nearby landmarks, sunset math, and a 3×3 bingo grid.
  6. Click **Print Field Card (A5)**.
* **Voiceover**:
  > *"Most outdoor apps trap you in a screen before you even tie your shoes. With Fieldcard, you spend fifteen seconds picking where you are and how long you have—then you hit print."*

---

### [0:15 – 0:30] Leaving the Screen Behind
* **Visual**: Physical camera view of an A5 printed page gliding out of the printer tray. Hand grabs the warm paper. Laptop lid shuts. Phone slips into a pocket.
* **Voiceover**:
  > *"The printer does the work. Phone in pocket. Screen closed. Zero signal needed on the trail."*

---

### [0:30 – 1:00] On the Trail with Paper
* **Visual**: Walking down a leaf-strewn dirt path. Holding the A5 card in hand.
* **Action**:
  1. The card points 0.4 km northwest toward The Ramble.
  2. Pausing by an oak tree.
  3. Reaching down to pick up an acorn with its rough cap intact (Item 3 on the bingo grid).
  4. Marking the box with a pencil.
* **Voiceover**:
  > *"The card gives you a mission and a map of real ground, but leaves your eyes up. No notifications, no GPS battery drain, no turn-by-turn voice in your ear. Just nine physical things to find with your bare hands and eyes."*

---

### [1:00 – 1:15] The Turn-Around Rule & Sunset
* **Visual**: Looking up at the afternoon sun filtering through the canopy. Checking the bold stats at the top of the card: `Turn around: 4:15 pm`.
* **Action**: Turning back toward home along a different winding path as the sun lowers.
* **Voiceover**:
  > *"Code does all the safety math. NOAA solar equations compute your sunset and turn-around time deterministically, so you never get caught in the dark."*

---

### [1:15 – 1:30] The Post-Walk Field Log
* **Visual**: Stepping back through the doorway. Setting down the pencil-marked card on the desk.
* **Action**: In the terminal, typing:
  ```bash
  python fieldcard.py log --found "acorn with cap, yellow leaf in mud, spider web" --notes "Sun dipped low through the oaks just before turn-around."
  ```
  Appends seamlessly to your lifelong `fieldnotes.md` seasonal journal.
* **Voiceover**:
  > *"When you return, one quick command logs your observations into your lifelong seasonal journal. Sixty minutes outside, fifteen seconds on screen. Fieldcard gets people back into the world."*
