# HydroSentry-AI — Basin Command Console

A formal, light-theme interface for **HydroSentry-AI**, a physics-guided flood &
drought early-warning system for the **Upper Bhima Basin (Pune, Maharashtra)**.

> **This is a live simulation.** The dashboard is driven by a self-contained
> **physics + statistics engine** ([`hydro_engine.py`](hydro_engine.py)) — a gamma
> unit hydrograph and reservoir mass balance for the flood side, and a
> soil-moisture bucket with evaporative-stress percentiles for the drought side.
> Every gauge, chart, table and directive is *computed*, not hard-coded. It runs
> **fully offline** — no data feeds, no APIs, no GPU, no heavy ML.

---

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

It opens in your browser at `http://localhost:8501`. No internet or account needed.

---

## Deploy on Render (live public URL)

This repo ships a [`render.yaml`](render.yaml) Blueprint, so Render can build and
host the app with no manual configuration:

1. Push this repo to GitHub (this branch/`main` already contains `render.yaml`).
2. Go to **[dashboard.render.com](https://dashboard.render.com)** and sign in with GitHub.
3. Click **New +  →  Blueprint**.
4. Select the **`KUNAL2007-maker/HydroSentry-AI`** repository and click **Apply**.
5. Render reads `render.yaml`, installs `requirements.txt`, and starts Streamlit
   bound to its `$PORT`. First build takes a few minutes; then you get a public
   `https://hydrosentry-ai.onrender.com`-style URL.

Every later `git push` to the tracked branch auto-redeploys. The **free** plan
sleeps after ~15 min of inactivity and wakes on the next request (first hit is slow).

---

## Scenarios & playback

The sidebar drives a live clock over a 32-step event window (an 8-hour flood
horizon and a 14-day drought horizon, advanced together):

- **Scenario** — *Normal operations*, *Flash flood*, *Flash drought*, or
  *Dipole crisis* (flood in the west and drought in the east at once).
- **Live simulation** — auto-advances the clock; a Streamlit `fragment(run_every=…)`
  re-renders the gauges, charts, tables and alerts on every tick, then stops
  automatically at the end of the event.
- **Step ▶ / Restart ↻** — advance one step by hand, or replay from the start.

As the clock runs, the reservoir fills and pre-releases, the downstream stage
rises toward the levee crest, soil moisture dries toward the wilting point, and
the per-stakeholder directives change with the computed severity.

---

## What's inside

Five tabs, each written for a non-technical reader first:

| Tab | For | Shows |
| --- | --- | --- |
| **Overview** | Everyone | The dipole (flood + drought) status, headline performance, active directives |
| **Farmer advisory** | Farmers | Flash-drought warning, evaporative-stress gauge, root-zone moisture trend, the Marathi/English SMS |
| **Reservoir operations** | Dam operators | FIRO pre-release directive, storage gauge, inflow forecast, live gate schedule |
| **Disaster response** | Civic teams | Geofenced evacuation directive, affected-zone table, map placeholder |
| **Model & validation** | Judges / reviewers | How it compares, validation scorecard, the four engines explained |

---

## What the engine actually computes

`hydro_engine.py` exposes three pure functions of `(scenario, tick)`:

- **`simulate(scenario, tick) -> BasinState`**
  - *Flood / reservoir:* rainfall pulse → runoff → gamma unit hydrograph inflow →
    reservoir mass balance (`dS/dt = inflow − release`) → storage, level, spill →
    FIRO pre-release buffer → downstream stage, time-to-overtopping and depth.
  - *Drought / agriculture:* temperature-driven PET → soil-moisture bucket with
    drainage and actual ET → Evaporative Stress Ratio → percentile vs climatology
    (ESP) → days-to-wilting.
- **`make_directives(state) -> dict`** — turns the state into plain-language,
  per-stakeholder directives (farmer, dam operator, disaster team) plus the SMS
  body and the overview activity feed.
- **`gate_schedule(state) -> list`** — a forward-looking gate plan whose release
  and reservoir-level columns are integrated with the *same* mass balance, so the
  action, release and level always agree.

Because everything is a pure function of `(scenario, tick)`, the UI can render any
moment, step forward, or replay — and real models can later replace `simulate()`
without touching the dashboard.

---

## Files

```
PCCOE HYDRO/
├── app.py                 # the dashboard (Streamlit, organised by tab)
├── hydro_engine.py        # the offline physics + statistics engine
├── requirements.txt       # streamlit + plotly + numpy
├── render.yaml            # Render Blueprint (one-click cloud deploy)
├── README.md              # this file
├── .streamlit/
│   └── config.toml        # light theme + brand colours
└── .claude/
    └── launch.json        # preview config (streamlit on port 8501)
```

---

## Replacing the simulation with real models

`simulate()` is the single seam. Swap its internals for the production models —
a PINN flood solver, an MC-LSTM-PET drought forecaster, the Errorcastnet error
model and the Nugen directive layer — while keeping the `BasinState` fields the
dashboard reads. The design tokens (colours) live in the `C = {...}` dict and the
`:root` CSS block so the look stays consistent as components are added.

> **Note on the numbers.** Reservoir capacity and a few basin constants are
> illustrative, chosen so the values stay internally consistent and close to the
> project's narrative figures. This is a demonstration engine, not an operational
> forecast.

---

*Indradhanu 2026.*
