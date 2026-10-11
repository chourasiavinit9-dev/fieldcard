# Prompt Engineering & Model Evaluation Notes

## 1. Design Strategy
The open-weight model writes the creative narrative for the field card: title, mission, sensory observation bingo, and send-off.
Our design rule is strict: **The model writes poetry, code does safety math.**
The model is forbidden from generating distances, turn-around times, sunset estimates, or inventing place names.

### Banned Clichés
Small open-weight models (1B–3B parameters) are prone to generic AI marketing speak. We enforce a zero-tolerance blacklist in both system prompting and post-generation filtering:
`"embrace"`, `"journey"`, `"tapestry"`, `"immerse"`, `"unplug"`, `"nature's beauty"`, `"breathtaking"`, `"serene"`, `"vibrant"`, `"nestled"`, `"delve"`

### Few-Shot Anchoring
Prompting with two concrete few-shot examples demonstrated an immediate jump in sensory fidelity and schema compliance:
```json
{
  "title": "Sixty Minutes in the Oaks",
  "mission": "Follow the earthen trail into the Ramble. Stand motionless beside the water until the ripple circles vanish.",
  "notice": [
    "A yellow oak leaf pinned in mud",
    "Spider web strung between low twigs",
    "Acorn with its rough cap intact",
    "Fungus like white stairs on bark",
    "Cold stone on the shadowed bank",
    "Bird with a berry in its beak",
    "Tree root curling across the dirt",
    "Sound of dry grass brushing ankles",
    "Bark smelling like wet cellar"
  ],
  "send_off": "Phone in pocket. Shoulders down."
}
```

---

## 2. Before vs. After Evaluations

| Prompt Version | Prompt Tone / Content | Observation Quality | Failure Modes |
|---|---|---|---|
| **V1 (Zero-shot, open prompt)** | "Embrace your journey into nature's tapestry." | "Notice birds singing", "Look at colorful flowers", "Observe the trees" | Hallucinated fictional parks; produced generic, passive items; ignored word limits. |
| **V2 (Constrained with few-shot & banned words)** | "Sixty Minutes in the Oaks. Follow the earthen path to the Ramble." | "A yellow oak leaf pinned in mud", "Spider web strung between low twigs", "Acorn with rough cap intact" | 100% valid JSON, strict length bounds, zero hallucinated landmarks, grounded in physical touch/sight/sound. |

---

## 3. Comparative Model Notes (Ollama)

* **Llama 3.2 3B (`llama3.2:3b`)**:
  * *Strengths*: Exceptional compliance with JSON format constraint; concise, sensory phrasing; respects negative constraints against cliches.
  * *Latency*: ~1.8 seconds on Apple Silicon.
  * *Rating*: **Recommended Default**.
* **Gemma 2 2B (`gemma2:2b`)**:
  * *Strengths*: Highly poetic, evocative descriptions of natural light, fog, and seasonal colors.
  * *Latency*: ~1.4 seconds.
  * *Rating*: **Top Partner Model Pick**.
* **Qwen 2.5 3B (`qwen2.5:3b`)**:
  * *Strengths*: Flawless schema adherence; very grounded sensory prompts.
  * *Latency*: ~1.9 seconds.
