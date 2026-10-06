# HydroSentry-AI — Basin Command Console

**▶ Live demo — [hydrosentry-ai.onrender.com](https://hydrosentry-ai.onrender.com/)**

[![Live demo](https://img.shields.io/website?url=https%3A%2F%2Fhydrosentry-ai.onrender.com&up_message=online&down_message=asleep&label=live%20demo&logo=render&logoColor=white)](https://hydrosentry-ai.onrender.com/)
[![Deploy to Render](https://img.shields.io/badge/Deploy-Render-46E3B7?logo=render&logoColor=white)](https://render.com/deploy?repo=https://github.com/KUNAL2007-maker/HydroSentry-AI)
[![Built with Streamlit](https://img.shields.io/badge/Built%20with-Streamlit-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org)
![Runs offline](https://img.shields.io/badge/Engine-offline%20physics%20%2B%20stats-0E7C8B)

A formal, light-theme interface for **HydroSentry-AI**, a physics-guided flood &
drought early-warning system for the **Upper Bhima Basin (Pune, Maharashtra)**.

> **Two modes, one console.** The console **opens in live mode** and a sidebar switch
> flips between:
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

The console **starts in 🛰️ Live data**, running on **real, current observations**
for the selected basin (Upper Bhima / Pune, 18.52°N 73.86°E by default) instead of a
scripted scenario. While the first reading is in flight you get a spinner, and if the
feed cannot be reached you get an honest *"live data unavailable"* panel with a retry
— the app never quietly serves the demo as if it were live. The demo stays one click
away: the toggle is pinned at the top of the sidebar so you can switch mid-presentation.

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
| **AI analyst** | Judges / reviewers | A question box answered from the state the page just computed, the fields it read, the harness that checked the answer, the briefing layer's spend — and the engine's validation evidence one expander down |

---

## Architecture — two clearly separated layers

HydroSentry-AI is deliberately split in two, and the console never mixes them.

### Layer 1 — Operational Production Engine (what actually runs)

Deterministic physics and statistics in [`hydro_engine.py`](hydro_engine.py), solved
on the CPU with no model weights, no GPU and no network call:

| Component | Method |
| --- | --- |
| Catchment routing | **Gamma unit-hydrograph convolution** of the effective-rainfall pulse |
| Reservoir | **Explicit Euler mass balance**, `dS/dt = inflow − release`, `dt = 0.1 h`, with the FIRO pre-release rule |
| Downstream risk | Rating curve vs levee crest → overtopping depth and time-to-impact |
| Drought | Soil-moisture bucket → **FAO-56 Evaporative Stress Ratio** → percentile climatology → days-to-wilting |
| Directives | Explicit, auditable rules over the computed state (severity thresholds, FIRO triggers, CWC Stage-1 watch) |

It is fast enough to re-run on every tick, so its execution time is **measured**
with `time.perf_counter()` and shown on the Overview tab as *Operational Real-Time
Engine Execution* — a real figure for that update, not one quoted from a paper.
On a current laptop a full `simulate()` call lands around **0.4 ms**; a shared
cloud CPU is slower, so treat the displayed number as the measurement and this
one as indicative.

```
Live observations (Open-Meteo) → Forcing → Gamma UH convolution
  → Euler reservoir mass balance → Levee stage & time-to-impact
  → FAO-56 ESR / ESP → BasinState → Rule-based directives → Dashboard
```

### Layer 2 — Research & Neural Surrogates (does not produce a number)

Layer 2 has two tracks, and neither one computes a value the console displays.

The **research track** targets what the closed-form engine deliberately does not
attempt — full 2D inundation mapping and learned error correction: a **PINN**
flood-map surrogate, an **MC-LSTM-PET** drought forecaster and **Errorcastnet**
bias correction. None of it executes to render the console.

The **language track** is the **Nugen** layer, and it does run — on every update, in
the product. It writes the per-role briefings and it answers an operator's questions,
and in neither job does it produce a number. It is described in full below.

The headline **~82.9 s** figure belongs to the research track: it is the PINN
surrogate's benchmark for producing a 2D depth map, against **~2.3 h** for an
equivalent HEC-RAS 2D run (~100× faster). **It is a research benchmark, not the
runtime of the operational engine.**

```
Historical / synthetic events → PINN 2D Saint-Venant surrogate → ~82.9 s depth map
  → MC-LSTM-PET drought forecaster → Errorcastnet bias correction
  → Validation vs HEC-RAS 2D & gauges → Research notebooks
```

Research-track components are staged behind the same `simulate()` seam, so each can
be promoted into Layer 1 once it is validated against gauge records.

### The Nugen language layer

A directive is written once but read by three different people. The duty engineer
wants gate settings, the farmer wants to know whether to irrigate, and the disaster
officer wants to know who to move and when. Every role tab therefore carries a
**Nugen briefing** ([`nugen_client.py`](nugen_client.py)) beneath its directive card:
the same decision the rule engine already made, re-worded for the reader that tab is
for. The same layer also answers questions on the AI analyst tab — still only from
numbers the engine computed, and still never computing one. See
**[Asking the console a question](#asking-the-console-a-question)**.

The boundary is the whole design. The layer is handed the numbers Layer 1 computed
and instructed to use nothing else; every figure it prints is checked back against
the computed state and anything unsupported is flagged on the card itself. It cannot
reach the physics engine, the directive cards remain the authoritative wording, and
**the console renders identically with the layer switched off** — a language model is
a good writer and a bad hydrologist, so it is given the writing and kept away from the
hydrology.

**Two models, read from the account's own catalogue.** The endpoint addresses a base
model by its *exact* Hugging Face id; a vendor-style alias (`qwen-v2p5-0p5b-instruct`)
is answered with a 404, which is how this layer broke once. The documented default is
therefore `Qwen/Qwen2.5-0.5B-Instruct`, and `resolve_models()` reads
`GET /api/v3/models/base?inference_ready=true&type=text-generation` once per process
and picks from what this account can actually call: the **smallest** instruct model for
the role briefings, which are a paraphrase, and the **largest** one under a 34B ceiling
for the analyst, which has to understand a question rather than re-word a sentence.
Both fall back to the default, so an unreachable or empty catalogue costs nothing but
the larger model. That listing is not an inference request and is not charged against
the call budget, and a 404 naming the model buys one catalogue re-read and one retry —
no more, so a genuinely missing model cannot turn every request into two.

```bash
export NUGEN_API_KEY="your-key"     # or .streamlit/secrets.toml (gitignored)
```

On Render, set `NUGEN_API_KEY` under **Environment → Environment Variables** — not in
`render.yaml`, which is public. The key is never committed.

**Quota discipline is built in**, because a layer that fetches as the basin changes
has to be cheap by construction rather than by good intentions:

| Device | Effect |
| --- | --- |
| Digest-keyed cache | One distinct situation costs at most one call per role, however many times the page re-renders |
| No wall clock in the digest, every reading rounded | Ordinary feed jitter (31.4 → 31.5 mm/hr) is not mistaken for news and re-billed |
| `AUTO_CALL_BUDGET` (18) below `CALL_BUDGET` (60) | Automatic briefings cannot consume the session; the headroom is what a deliberate briefing and the interactive analyst answer out of |
| Demo mode never auto-fires | 33 scripted ticks at a 2 s refresh would drain a budget in under a minute and say nothing new |
| Background thread, never inline | A 30 s read timeout cannot freeze the console on the updates that matter |
| 500-token hard cap — 150 per role briefing, 300 per analyst answer | Bounded cost per call |

The **AI analyst** tab is the layer's audit surface: the exact prompt sent for every
role, the exact prompt sent for a question, the calls actually spent against the
allowance, and the figure check. `verify_app.py` test I asserts this policy against a
stubbed transport — no key means no call, the demo never fetches, and three live
renders of an unchanged basin spend three calls, not nine.

### Asking the console a question

The analyst is that same layer doing a second job, and it is the one job where a
language model could do real damage. Asked *"how long until the levee goes under?"*
with the whole console in its context, a model answers fluently and sometimes wrongly,
and a wrong number in a flood warning is the worst output this project can produce. So
the model is never the thing that reads the data. The question box on tab 5 runs six
stages, and the tab prints what each one did with your question as an **AI harness**
table:

| Stage | What it does |
| --- | --- |
| **Scope** | The question is matched against the topics this console actually holds. No match means no model call: the honest answer is *"this console does not hold that"*, which costs nothing and cannot be wrong |
| **Retrieve** | The matched topics select state fields **by name** — ordinary attribute lookup, exact by construction. The fields read are shown to the operator in an evidence table |
| **Ground** | A rule-built answer is composed from those fields **before any model runs**. This is the answer of record |
| **Generate** | Nugen is handed the question and those fields, and nothing else |
| **Verify** | Every figure in the reply is checked back against the retrieved facts |
| **Fall back** | A failed, degenerate or unverified reply is discarded and the computed answer stands |

Grounding before generating is what makes the accuracy claim true rather than hopeful:
the figure the operator reads was computed by the engine and selected by name, and the
model's only job is the English around it. A second button answers from the data alone
with no model call at all, and that is also what a missing key looks like — the
computed answer appears either way, and the model's absence is reported as a stage
outcome rather than as an error.

A field can only reach the analyst by appearing in the registry in
[`nugen_client.py`](nugen_client.py), so that one tuple is the whole retrieval surface:
there is no path from a question to a value that does not pass through it. Two details
in it were learned the hard way. Keyword matching is on word boundaries rather than
bare substrings, because `gate` inside *irri-gate* quietly routed every farmer question
through the reservoir-release fields. And retrieved fields are **ranked** by how many
of the question's topics they cover before the list is truncated, because registry
order once pushed time-to-overtopping off the end of the sheet for *"how long until the
levee overtops"* — the one field the question was about.

The validation evidence that used to be this tab is all still here, one level down in
an **Engine validation** expander: the mass-balance proof, the determinism
byte-compare, the runtime spread, the rule audit trail, the rainfall sensitivity sweep
and the labelled reference constants.

### How it compares to conventional processing

The tab closes with a five-row comparison against conventional 2D modelling — time to
a decision, what it runs on, re-running it, what comes out, and the language layer. It
is deliberately not a tab of its own: a comparison is a closing remark, not a finding.

Only the first row needs care, and the table says so on screen. The left-hand figure is
this engine's own `perf_counter()` reading for the update on display; **~2.3 h is a
published benchmark for an equivalent HEC-RAS 2D run, not something measured here.**
The two solve different-sized problems: the closed-form chain answers *how high, how
soon, who is exposed* and finishes in milliseconds, while a 2D solver produces a full
inundation surface this engine does not attempt. The honest claim is not that
HydroSentry is a faster 2D model — it is that the decision a control room needs does
not require one.

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
├── nugen_client.py        # Nugen layer: role briefings + grounded analyst (needs a key)
├── verify_demo_golden.py  # regression guard: demo output must stay byte-identical
├── verify_fixes.py        # acceptance checks: clock, directives, zones, timing
├── verify_app.py          # Streamlit AppTest checks (modes, failure path, briefing layer)
├── requirements.txt       # streamlit + plotly + numpy + requests
├── render.yaml            # Render Blueprint (one-click cloud deploy)
├── README.md              # this file
├── .streamlit/
│   └── config.toml        # light theme + brand colours
└── .claude/
    └── launch.json        # preview config (streamlit on port 8501)
```

Verify a change before pushing:

```bash
python -m py_compile app.py hydro_engine.py live_data.py nugen_client.py
python verify_demo_golden.py   # must print 132/132 states identical
python verify_fixes.py
python verify_app.py
```

---

## Replacing the simulation with real models

`simulate()` is the single seam. It accepts either a demo scenario **or** a
`Forcing` built from live observations (`forcing_from_live`), so real data already
flows through it today. Swap its internals for the production models — a PINN flood
solver, an MC-LSTM-PET drought forecaster and the Errorcastnet error model — while
keeping the `BasinState` fields the dashboard reads. The Nugen language layer sits
*outside* this seam by design: it reads the finished `BasinState` and directives and
writes prose, so promoting a research model into Layer 1 changes what the briefings
describe without changing the briefing layer at all.
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
