# HydroSentry-AI — Basin Command Console

**▶ Live demo — [hydrosentry-ai.onrender.com](https://hydrosentry-ai.onrender.com/)**

[![Live demo](https://img.shields.io/website?url=https%3A%2F%2Fhydrosentry-ai.onrender.com&up_message=online&down_message=asleep&label=live%20demo&logo=render&logoColor=white)](https://hydrosentry-ai.onrender.com/)
[![Deploy to Render](https://img.shields.io/badge/Deploy-Render-46E3B7?logo=render&logoColor=white)](https://render.com/deploy?repo=https://github.com/KUNAL2007-maker/HydroSentry-AI)
[![Built with Streamlit](https://img.shields.io/badge/Built%20with-Streamlit-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org)
![Runs offline](https://img.shields.io/badge/Engine-offline%20physics%20%2B%20stats-0E7C8B)

A formal, light-theme interface for **HydroSentry-AI**, a physics-guided flood &
drought early-warning system for the **Upper Bhima Basin (Pune, Maharashtra)**.

> **Two modes, one console.** A sidebar switch flips between:
> - **🎬 Demo mode** — a self-contained **physics + statistics engine**
>   ([`hydro_engine.py`](hydro_engine.py)): a gamma unit hydrograph and reservoir
>   mass balance for the flood side, a soil-moisture bucket with evaporative-stress
>   percentiles for the drought side. Every gauge, chart, table and directive is
>   *computed*, not hard-coded, and it runs **fully offline** — no feeds, no GPU.
> - **🛰️ Live data** — the *same* engine driven by **real-time observations**
>   ([`live_data.py`](live_data.py)) for the basin: live rainfall, temperature,
>   root-zone soil moisture and evapotranspiration. Keyless by default (Open-Meteo),
>   with a pluggable seam for a keyed/official source. See **[Live data](#live-data)**.

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

**Embed it anywhere.** The deployed app is iframe-friendly (see
[`.streamlit/config.toml`](.streamlit/config.toml)). Drop it into any page and
append `?embed=true` to hide the Streamlit menu and footer:

```html
<iframe src="https://hydrosentry-ai.onrender.com/?embed=true"
        width="100%" height="900" style="border:0"></iframe>
```

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

## Live data

Flip the sidebar to **🛰️ Live data** and the console runs on **real, current
observations** for the Upper Bhima Basin (Pune, 18.52°N 73.86°E) instead of a
scripted scenario. The demo stays one click away — the toggle is pinned at the
top of the sidebar so you can switch back mid-presentation.

**What goes live** (the same `BasinState`, so every tab, gauge and directive just
works):

| Signal | Source | Feeds |
| --- | --- | --- |
| Rainfall now + next-hours peak | Open-Meteo hourly `precipitation` | flood inflow magnitude & timing |
| Temperature / heat | `temperature_2m`, daily `temperature_2m_max` | drought heat stress |
| Root-zone soil moisture | `soil_moisture_9_to_27cm` | real drought state (ESR/ESP, days-to-wilting) + trend chart |
| Evapotranspiration | `et0_fao_evapotranspiration` | real atmospheric demand (PET) |
| **Current reservoir %** | **manual operator slider** | reservoir mass-balance start |

Khadakwasla storage has no free public live feed, so the *current* reservoir level
is a labelled operator slider; the rainfall-driven **forecast and pre-release are
computed live**. This is stated honestly in the UI.

**Keyless by default.** Live mode uses [Open-Meteo](https://open-meteo.com) — a
free public API that needs no key and speaks HTTPS (so it works on Render and inside
an iframe). Nothing to configure.

**Pluggable source.** To wire in a keyed/official feed (IMD, a college endpoint, a
paid API…), implement `CustomProvider.fetch` in [`live_data.py`](live_data.py) and
select it at runtime:

```bash
export HYDRO_DATA_PROVIDER=custom
export HYDRO_API_URL="https://your.endpoint/..."
export HYDRO_API_KEY="your-key"
```

**Never crashes.** `fetch_live()` wraps the network in try/except and is cached for
120 s (the **↻ Refresh now** button forces a fresh pull). If the fetch fails, the
console shows a clean *"data unavailable — fallback"* badge and a calm neutral state
instead of an error.

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
├── hydro_engine.py        # the physics + statistics engine (demo + live)
├── live_data.py           # real-time basin fetch (Open-Meteo, pluggable)
├── requirements.txt       # streamlit + plotly + numpy + requests
├── render.yaml            # Render Blueprint (one-click cloud deploy)
├── README.md              # this file
├── .streamlit/
│   └── config.toml        # light theme + brand colours
└── .claude/
    └── launch.json        # preview config (streamlit on port 8501)
```

---

## Replacing the simulation with real models

`simulate()` is the single seam. It accepts either a demo scenario **or** a
`Forcing` built from live observations (`forcing_from_live`), so real data already
flows through it today. Swap its internals for the production models — a PINN flood
solver, an MC-LSTM-PET drought forecaster, the Errorcastnet error model and the
Nugen directive layer — while keeping the `BasinState` fields the dashboard reads.
New live signals plug in the same way: extend `LiveObs` in [`live_data.py`](live_data.py)
and map them in `forcing_from_live`. The design tokens (colours) live in the
`C = {...}` dict and the `:root` CSS block so the look stays consistent as
components are added.

> **Note on the numbers.** Reservoir capacity and a few basin constants are
> illustrative, chosen so the values stay internally consistent and close to the
> project's narrative figures. This is a demonstration engine, not an operational
> forecast.

---

*Indradhanu 2026.*
