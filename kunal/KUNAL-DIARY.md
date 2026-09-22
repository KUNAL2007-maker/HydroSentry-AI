# 📔 Kunal's Diary — HydroSentry-AI in Very Simple Words

Hi! This is a simple diary that explains what our website does, in plain
language. No hard words. If you read this top to bottom, you will understand
the whole project. 🙂

---

## 1. What is this website?

It is a **warning screen for floods and droughts** for the area around Pune
(the Upper Bhima Basin).

Think of it like a **weather-alert dashboard**, but smarter. It watches two
dangers at the same time:

- 🌊 **Flood** — too much water (a dam might overflow, a river might flood houses).
- 🌵 **Drought** — too little water (the soil dries up and crops die).

The clever part: this region can get **both at once** (flood on one side,
drought on the other). We call this a **"dipole crisis."** Our website is built
to handle both together.

---

## 2. Is the data real?

No — and this is important. **Everything you see is calculated live by a small
maths engine on your own computer.** There is no internet, no live sensors, no
big AI model. It runs fully offline.

- The maths engine lives in the file `hydro_engine.py`.
- The screen (buttons, charts, colours) lives in the file `app.py`.

The engine uses **real physics formulas** (how water fills a dam, how soil
dries out), so the numbers make sense and change realistically. It is a
**demonstration/simulation** — like a very realistic practice model.

---

## 3. The 4 Scenarios (you pick one on the left)

On the left side (the "sidebar") you can choose a situation to watch:

| Scenario | What happens |
| --- | --- |
| **Normal operations** | Calm day. Light rain, healthy soil. Nothing scary. |
| **Flash flood** | A sudden heavy cloudburst. Water rushes into the dam fast. |
| **Flash drought** | A heatwave dries the fields quickly. |
| **Dipole crisis** | The big one — flood in the west **and** drought in the east at the same time. |

When you pick a scenario, the whole screen updates to show that situation.

---

## 4. The Playback controls (like a video player) ▶️

The event is not one photo — it plays out over time (like a short movie of
32 steps). You control it:

- **Live simulation (checkbox)** — turn it ON and the clock moves by itself.
  The charts, numbers and warnings update automatically, then stop when the
  event is over.
- **Seconds per step (slider)** — how fast the movie plays (1 to 5 seconds
  per step).
- **Step ▶ button** — move forward just one step by hand.
- **Restart ↻ button** — go back to the beginning and play again.

As the clock runs, you can watch the dam fill up, the river rise toward the
danger line, the soil dry out, and the warnings change from green to red.

Also on the left you can see:
- A **"Data sources"** list (the satellites/radar a real version would use).
- A **colour key**: 🟢 Normal, 🟡 Watch, 🟠 Elevated, 🔴 Critical.

---

## 5. The 5 Tabs (the main pages) 📑

At the top there are 5 tabs. Each one is written for a different person.

### Tab 1 — 🗺️ Overview (for everyone)
The big-picture summary. It shows:
- Two boxes side by side: **Flood status** and **Drought status**.
- A row of **score cards** (how fast and accurate the system is).
- A **live feed of active warnings** (newest instructions).
- A small basin map area.

### Tab 2 — 🌾 Farmer advisory (for farmers)
Helps farmers save their crops before it is too late. It shows:
- A clear instruction, e.g. *"Start drip irrigation at 4 AM."*
- An **SMS message in Marathi and Hindi** that would be sent to farmers.
- A **gauge** showing how thirsty the air is (evaporative stress).
- A **line chart** of how wet/dry the soil is over the last days.

### Tab 3 — 💧 Reservoir operations (for dam operators)
Helps the dam team decide when to let water out. It shows:
- The main advice, e.g. *"Release some water now to make room for the flood."*
- A **gauge** of how full the dam is right now (%).
- A **chart** of how much water is coming in over the next 6 hours.
- A **timed gate schedule** (a to-do plan: what to do, when, and the water level).

The main idea here: **let a little water out early** so the dam does not
suddenly overflow later. This is called **FIRO** (forecast-informed release).

### Tab 4 — 🚨 Disaster response (for rescue/civic teams)
Helps decide who to evacuate. It shows:
- The main order, e.g. *"Evacuate Sectors 4 and 5 within X minutes."*
- A **table of affected zones** (which streets, how many houses, how much time).
- A **risk map** area.
- Key numbers: time to impact, how high the river will rise, houses at risk,
  shelters ready.

The smart part: it only warns the **exact streets** that are in danger — not
the whole city — so there is no unnecessary panic.

### Tab 5 — 📊 Model & validation (for judges / tech reviewers)
Explains why the system can be trusted. It shows:
- A **comparison table** vs other tools (our system is fast **and** obeys physics).
- A **scorecard** of accuracy numbers (KGE, detection rate, etc.).
- A **"honesty test"** — proof our model does not invent fake water when it
  gets very hot.
- Simple cards explaining the **four engines** behind the project.

---

## 6. What the maths engine actually does (super simple)

The engine takes two inputs — **which scenario** and **which time step** — and
calculates everything from them.

**For floods / the dam:**
1. Rain falls → water runs off into the river. 🌧️
2. Water flows into the dam (rises, then falls — like a wave).
3. Water in minus water out = how full the dam gets.
4. It works out when the river would overflow the flood wall, and how many
   houses that puts at risk.

**For drought / the soil:**
1. Heat makes the air "thirsty" (it wants to pull up water). ☀️
2. The soil is like a bucket that slowly loses water.
3. It compares today's dryness to normal history (a percentile).
4. It predicts how many days until the crops start to wilt.

Then a **rules layer** turns all these numbers into plain sentences for each
person (farmer, dam operator, rescue team) and writes the SMS text.

Because it is all just formulas from `(scenario, time)`, the movie can play
forward, step, or replay perfectly every time.

---

## 7. How to run it on your computer 💻

Open a terminal in the project folder and type:

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open **http://localhost:8501** in your browser. No internet needed.

---

## 8. The files, in one line each 🗂️

- **`app.py`** — the screen you see (buttons, tabs, charts, colours).
- **`hydro_engine.py`** — the maths brain that calculates all the numbers.
- **`requirements.txt`** — the 3 tools it needs (streamlit, plotly, numpy).
- **`README.md`** — the official, more technical explanation.
- **`.streamlit/config.toml`** — the light colour theme.
- **`KUNAL-DIARY.md`** — this friendly file you are reading now. 😄

---

*That's the whole project in simple words. — Kunal's Diary, Indradhanu 2026.*
