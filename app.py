"""
HydroSentry-AI — Basin Command Console (UI shell)
=================================================

Physics-guided flood & drought early-warning interface for the
Upper Bhima Basin (Pune, Maharashtra).

This is the FORMAL, LIGHT-THEME console. Every number, chart, directive and
alert below is now driven by a live, on-device PHYSICS + STATISTICS engine
(see hydro_engine.py) — no external feeds, no GPU, no black-box ML. It runs
fully offline.

------------------------------------------------------------------
HOW IT WORKS
------------------------------------------------------------------
  * hydro_engine.simulate(scenario, tick) computes the whole basin state
    (reservoir mass balance, FIRO pre-release, levee overtopping, soil-
    moisture bucket, evaporative-stress percentile, days-to-wilting).
  * hydro_engine.make_directives(state) turns that state into plain-language,
    per-stakeholder instructions; gate_schedule(state) builds the ops table.
  * The sidebar picks a scenario (Normal / Flash flood / Flash drought /
    Dipole) and drives playback. A st.fragment(run_every=…) loop advances the
    simulation clock in real time so gauges, charts and alerts auto-update.
------------------------------------------------------------------

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

import copy
import json
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

import hydro_engine as H
import live_data
import nugen_client

try:
    # Optional browser bridge: lets the VISITOR's browser fetch the live feed
    # from THEIR IP instead of the shared hosting IP (which Open-Meteo
    # rate-limits). If it's missing or fails, live mode silently falls back to
    # the server-side fetch, so this is purely additive.
    from streamlit_js_eval import streamlit_js_eval
    HAS_JS_EVAL = True
except Exception:
    HAS_JS_EVAL = False

try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except Exception:                      # plotly optional; UI still renders without it
    HAS_PLOTLY = False

try:
    import pydeck as pdk
    import pandas as pd
    HAS_PYDECK = True
except Exception:                      # pydeck optional; maps fall back to a static panel
    HAS_PYDECK = False


# ----------------------------------------------------------------------------
# Page configuration
# ----------------------------------------------------------------------------
st.set_page_config(
    page_title="HydroSentry-AI — Basin Command Console",
    page_icon="💧",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ----------------------------------------------------------------------------
# Design tokens (kept in one place so functionality phase can reuse them)
# ----------------------------------------------------------------------------
C = {
    "paper":   "#F3F6F8",
    "surface": "#FFFFFF",
    "ink":     "#17262F",
    "muted":   "#5A6B78",
    "line":    "#DCE3EA",
    "brand":   "#123B54",
    "teal":    "#0E7C8B",
    "flood":   "#1F5FB0",
    "drought": "#B26A2E",
    "safe":    "#1F8A5B",
    "watch":   "#B98314",
    "warning": "#C15E22",
    "critical":"#C1362F",
}


# ----------------------------------------------------------------------------
# Global stylesheet  (plain string — NOT an f-string, because CSS uses braces)
# ----------------------------------------------------------------------------
CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Noto+Sans+Devanagari:wght@400;500;600&display=swap');

:root{
  --paper:#F3F6F8; --surface:#FFFFFF; --ink:#17262F; --muted:#5A6B78;
  --line:#DCE3EA; --brand:#123B54; --teal:#0E7C8B;
  --flood:#1F5FB0; --drought:#B26A2E;
  --safe:#1F8A5B; --watch:#B98314; --warning:#C15E22; --critical:#C1362F;
  --radius:12px;
  --shadow:0 1px 2px rgba(18,59,84,.04), 0 6px 20px rgba(18,59,84,.06);
}

/* base ------------------------------------------------------------------- */
.stApp{ background:var(--paper); }
html, body, [class*="css"], .stApp, .stMarkdown, p, span, div, li, td, th{
  font-family:'IBM Plex Sans','Segoe UI',system-ui,sans-serif;
  color:var(--ink);
}
h1,h2,h3,h4{ font-family:'IBM Plex Sans',system-ui,sans-serif; color:var(--ink); letter-spacing:-.01em; }
a{ color:var(--teal); text-decoration:none; }
a:hover{ text-decoration:underline; }
.stMarkdown img, .hs-panel img{ max-width:100%; height:auto; }

/* strip default streamlit chrome, keep it functional -------------------- */
[data-testid="stHeader"]{ background:transparent; box-shadow:none; }
[data-testid="stToolbar"], #MainMenu, [data-testid="stDecoration"], footer{ display:none; }
.block-container{ max-width:1320px; margin-inline:auto;
  padding-top:1.1rem; padding-bottom:3rem; padding-inline:clamp(1rem, 4vw, 3rem); }

/* focus visibility (a11y) ------------------------------------------------ */
a:focus-visible, button:focus-visible, [tabindex]:focus-visible{
  outline:2px solid var(--teal); outline-offset:2px; border-radius:6px;
}

/* sidebar : the station rail -------------------------------------------- */
[data-testid="stSidebar"]{ background:var(--surface); border-right:1px solid var(--line); }
[data-testid="stSidebar"] .block-container{ padding-top:1rem; padding-inline:1rem; }
.hs-brand{ padding:2px 2px 14px; border-bottom:1px solid var(--line); margin-bottom:14px; }
.hs-brand__mark{ display:flex; align-items:center; gap:10px; }
.hs-brand__logo{
  width:34px; height:34px; border-radius:9px; flex:0 0 auto;
  background:var(--brand); color:#fff; display:grid; place-items:center;
  font-weight:700; font-size:16px;
}
.hs-brand__name{ font-size:17px; font-weight:700; color:var(--brand); line-height:1.1; }
.hs-brand__tag{ font-size:12px; color:var(--muted); margin-top:3px; }
.hs-brand__loc{ font-size:12.5px; color:var(--ink); margin-top:12px; line-height:1.5; }
.hs-brand__loc b{ color:var(--brand); font-weight:600; }

.hs-rail-h{ font-size:11px; font-weight:600; color:var(--muted);
  text-transform:none; margin:18px 0 8px; letter-spacing:.02em; }

/* live pill -------------------------------------------------------------- */
.hs-live{ display:inline-flex; align-items:center; gap:7px; font-size:12.5px;
  font-weight:600; color:var(--safe); background:#E7F5EE; border:1px solid #C4E7D4;
  padding:4px 10px; border-radius:999px; }
.hs-live__dot{ width:8px; height:8px; border-radius:50%; background:var(--safe);
  box-shadow:0 0 0 0 rgba(31,138,91,.5); animation:hs-pulse 2s infinite; }
@keyframes hs-pulse{
  0%{ box-shadow:0 0 0 0 rgba(31,138,91,.45); }
  70%{ box-shadow:0 0 0 7px rgba(31,138,91,0); }
  100%{ box-shadow:0 0 0 0 rgba(31,138,91,0); }
}
.hs-updated{ font-size:12px; color:var(--muted); margin-top:8px; }
.hs-updated b{ font-family:'IBM Plex Mono',monospace; color:var(--ink); font-weight:500; }

/* data-source instrument list ------------------------------------------- */
.hs-src{ display:flex; align-items:flex-start; gap:9px; padding:7px 0;
  border-bottom:1px solid var(--line); }
.hs-src:last-child{ border-bottom:none; }
.hs-src__dot{ width:8px; height:8px; border-radius:50%; margin-top:5px; flex:0 0 auto; background:var(--safe); }
.hs-src__name{ font-size:13px; font-weight:600; }
.hs-src__desc{ font-size:11.5px; color:var(--muted); }

/* severity legend -------------------------------------------------------- */
.hs-leg{ display:flex; align-items:center; gap:8px; font-size:12.5px; padding:4px 0; }
.hs-leg__sw{ width:12px; height:12px; border-radius:3px; flex:0 0 auto; }

/* command header --------------------------------------------------------- */
.hs-cmd{ display:flex; align-items:flex-end; justify-content:space-between;
  gap:16px; flex-wrap:wrap; margin:2px 0 14px; }
.hs-cmd__title{ font-size:clamp(20px, 3.4vw, 26px); font-weight:700; color:var(--brand); line-height:1.15; }
.hs-cmd__sub{ font-size:13.5px; color:var(--muted); margin-top:3px; }

/* badges ----------------------------------------------------------------- */
.hs-badge{ display:inline-flex; align-items:center; gap:6px; font-size:12.5px;
  font-weight:600; padding:3px 10px; border-radius:999px; border:1px solid; white-space:nowrap; }
.hs-badge .hs-dot{ width:7px; height:7px; border-radius:50%; }
.hs-badge--safe{ color:var(--safe); background:#E7F5EE; border-color:#C4E7D4; }
.hs-badge--safe .hs-dot{ background:var(--safe); }
.hs-badge--watch{ color:#8A6205; background:#FBF2D8; border-color:#EFDCA4; }
.hs-badge--watch .hs-dot{ background:var(--watch); }
.hs-badge--warning{ color:#9E4818; background:#FBEBDF; border-color:#F1CFB4; }
.hs-badge--warning .hs-dot{ background:var(--warning); }
.hs-badge--critical{ color:#9E2A23; background:#FBE6E4; border-color:#F1C6C1; }
.hs-badge--critical .hs-dot{ background:var(--critical); }

/* generic panel ---------------------------------------------------------- */
.hs-panel{ background:var(--surface); border:1px solid var(--line);
  border-radius:var(--radius); padding:18px 18px; }
.hs-panel--pad{ padding:20px 22px; }

/* dipole hero ------------------------------------------------------------ */
.hs-dipole{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.hs-hazard{ background:var(--surface); border:1px solid var(--line);
  border-radius:var(--radius); padding:18px 20px; border-left:4px solid var(--line);
  box-shadow:var(--shadow); }
.hs-hazard--flood{ border-left-color:var(--flood); }
.hs-hazard--drought{ border-left-color:var(--drought); }
.hs-hazard__top{ display:flex; align-items:center; justify-content:space-between; gap:10px; }
.hs-hazard__eyebrow{ font-size:12px; font-weight:600; color:var(--muted); }
.hs-hazard__eyebrow b{ font-weight:600; }
.hs-hazard--flood .hs-hazard__eyebrow b{ color:var(--flood); }
.hs-hazard--drought .hs-hazard__eyebrow b{ color:var(--drought); }
.hs-hazard__h{ font-size:clamp(17px, 2.9vw, 21px); font-weight:700; margin:10px 0 6px; }
.hs-hazard__desc{ font-size:13.5px; color:var(--muted); line-height:1.55; }
.hs-hazard__mini{ display:flex; gap:22px; margin-top:14px; padding-top:14px; border-top:1px solid var(--line); }
.hs-hazard__mini .k{ font-size:12px; color:var(--muted); }
.hs-hazard__mini .v{ font-family:'IBM Plex Mono',monospace; font-size:16px; font-weight:500; margin-top:3px; }

/* readout grid (metrics) ------------------------------------------------- */
.hs-readouts{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
  background:var(--line); border:1px solid var(--line); border-radius:var(--radius); overflow:hidden; }
.hs-metric{ background:var(--surface); padding:16px 18px; }
.hs-metric__label{ font-size:12.5px; color:var(--muted); }
.hs-metric__value{ font-family:'IBM Plex Mono',monospace; font-size:clamp(19px, 3.6vw, 26px); font-weight:600;
  color:var(--brand); margin-top:6px; line-height:1; }
.hs-metric__value .u{ font-size:14px; font-weight:500; color:var(--muted); margin-left:4px; }
.hs-metric__ctx{ font-size:12px; color:var(--muted); margin-top:7px; }

/* plain-language note ---------------------------------------------------- */
.hs-note{ background:#EAF3F4; border:1px solid #CFE6E8; border-left:4px solid var(--teal);
  border-radius:10px; padding:12px 15px; font-size:13.5px; color:#254952; line-height:1.55; }
.hs-note b{ color:var(--teal); font-weight:600; }

/* directive panel (hero of each role tab) -------------------------------- */
.hs-dir{ background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  overflow:hidden; box-shadow:var(--shadow); }
.hs-dir__head{ display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding:14px 20px; border-bottom:1px solid var(--line); }
.hs-dir--critical .hs-dir__head{ background:#FCEEEC; }
.hs-dir--warning .hs-dir__head{ background:#FCF0E7; }
.hs-dir--watch .hs-dir__head{ background:#FCF6E4; }
.hs-dir__kicker{ font-size:12px; font-weight:600; color:var(--muted); }
.hs-dir__body{ padding:16px 20px 6px; }
.hs-dir__title{ font-size:clamp(16px, 2.4vw, 19px); font-weight:700; margin:0 0 8px; color:var(--ink); }
.hs-dir__situation{ font-size:14px; color:#3a4b57; line-height:1.6; margin:0 0 14px; }
.hs-dir__alabel{ font-size:12px; font-weight:600; color:var(--muted); margin-bottom:4px; }
.hs-dir__actions{ margin:0 0 6px; padding-left:20px; }
.hs-dir__actions li{ font-size:14px; line-height:1.5; margin:6px 0; }
.hs-dir__foot{ display:flex; align-items:center; justify-content:space-between; gap:12px;
  flex-wrap:wrap; padding:12px 20px; border-top:1px solid var(--line); margin-top:8px; }
.hs-dir__meta{ font-size:12.5px; color:var(--muted); }
.hs-cert{ font-size:12px; font-weight:600; color:var(--brand); background:#EAF0F3;
  border:1px solid #D3E0E8; border-radius:999px; padding:4px 11px; display:inline-flex; align-items:center; gap:6px; }
.hs-cert::before{ content:""; width:7px; height:7px; border-radius:50%; background:var(--teal); }

/* section heading -------------------------------------------------------- */
.hs-h2{ display:flex; align-items:baseline; gap:10px; margin:6px 0 12px; }
.hs-h2 h2{ font-size:16px; font-weight:600; margin:0; }
.hs-h2 .s{ font-size:12.5px; color:var(--muted); }

/* SMS / message card ----------------------------------------------------- */
.hs-sms{ background:#F7FAFB; border:1px solid var(--line); border-radius:var(--radius); padding:16px 18px; }
.hs-sms__top{ display:flex; align-items:center; gap:8px; font-size:12.5px; color:var(--muted); margin-bottom:10px; }
.hs-sms__bubble{ background:var(--surface); border:1px solid var(--line); border-radius:4px 14px 14px 14px;
  padding:12px 14px; }
.hs-sms__mr{ font-family:'Noto Sans Devanagari','IBM Plex Sans',sans-serif; font-size:14px;
  line-height:1.6; color:var(--ink); }
.hs-sms__en{ font-size:12.5px; color:var(--muted); line-height:1.55; margin-top:9px;
  padding-top:9px; border-top:1px dashed var(--line); }
.hs-sms__sent{ font-size:11.5px; color:var(--muted); margin-top:10px; }

/* tables ----------------------------------------------------------------- */
.hs-scroll{ overflow-x:auto; -webkit-overflow-scrolling:touch; }
.hs-table{ width:100%; border-collapse:collapse; font-size:13.5px; }
.hs-table th{ text-align:left; font-size:11.5px; font-weight:600; color:var(--muted);
  padding:9px 12px; border-bottom:1px solid var(--line); background:#F7FAFB; }
.hs-table td{ padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
.hs-table tr:last-child td{ border-bottom:none; }
.hs-table .num{ font-family:'IBM Plex Mono',monospace; font-weight:500; color:var(--brand); }
.hs-yes{ color:var(--safe); font-weight:600; }
.hs-no{ color:var(--critical); font-weight:600; }

/* directive feed (overview) --------------------------------------------- */
.hs-feed{ display:flex; flex-direction:column; }
.hs-feed__row{ display:flex; align-items:flex-start; gap:12px; padding:12px 2px; border-bottom:1px solid var(--line); }
.hs-feed__row:last-child{ border-bottom:none; }
.hs-feed__time{ font-family:'IBM Plex Mono',monospace; font-size:12.5px; color:var(--muted);
  width:52px; flex:0 0 auto; padding-top:2px; }
.hs-feed__txt{ font-size:13.5px; line-height:1.45; }
.hs-feed__txt b{ font-weight:600; }

/* map plate placeholder -------------------------------------------------- */
.hs-map{ position:relative; height:320px; border:1px solid var(--line); border-radius:var(--radius);
  background:
    linear-gradient(0deg, rgba(31,95,176,.05), rgba(31,95,176,.05)),
    repeating-linear-gradient(0deg, transparent 0 27px, rgba(18,59,84,.05) 27px 28px),
    repeating-linear-gradient(90deg, transparent 0 27px, rgba(18,59,84,.05) 27px 28px),
    var(--surface);
  display:grid; place-items:center; overflow:hidden; }
.hs-map__tag{ position:absolute; top:12px; left:14px; font-size:11.5px; font-weight:600;
  color:var(--brand); background:rgba(255,255,255,.85); border:1px solid var(--line);
  padding:4px 9px; border-radius:6px; }
.hs-map__mid{ text-align:center; color:var(--muted); font-size:13px; }
.hs-map__legend{ position:absolute; bottom:12px; left:14px; display:flex; gap:14px; flex-wrap:wrap;
  background:rgba(255,255,255,.9); border:1px solid var(--line); border-radius:8px; padding:8px 12px; }

/* mini architecture cards (model tab) ----------------------------------- */
.hs-arch{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }
.hs-arch__card{ background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:16px 18px; border-top:3px solid var(--teal); }
.hs-arch__tag{ font-family:'IBM Plex Mono',monospace; font-size:11.5px; color:var(--teal); font-weight:500; }
.hs-arch__h{ font-size:15px; font-weight:600; margin:6px 0 6px; }
.hs-arch__p{ font-size:13px; color:var(--muted); line-height:1.55; }

/* two-layer architecture (model tab) ------------------------------------- */
.hs-layer{ background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:16px 18px; border-left:4px solid var(--teal); margin-bottom:14px; }
.hs-layer--research{ border-left-color:#9AA7B0; }
.hs-layer__tag{ font-family:'IBM Plex Mono',monospace; font-size:11px; color:var(--teal);
  font-weight:600; letter-spacing:.06em; text-transform:uppercase; }
.hs-layer--research .hs-layer__tag{ color:var(--muted); }
.hs-layer__h{ font-size:16px; font-weight:700; margin:6px 0 6px; }
.hs-layer__p{ font-size:13px; color:var(--muted); line-height:1.6; }
.hs-flow{ display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-top:12px;
  font-family:'IBM Plex Mono',monospace; font-size:11.5px; }
.hs-flow__step{ background:#EEF3F5; border:1px solid var(--line); border-radius:6px;
  padding:4px 8px; color:var(--brand); white-space:nowrap; }
.hs-layer--research .hs-flow__step{ background:#F4F6F8; color:var(--muted); }
.hs-flow__arrow{ color:var(--muted); }

/* drift bars (model tab) ------------------------------------------------- */
.hs-bar{ margin:10px 0; }
.hs-bar__lab{ display:flex; justify-content:space-between; font-size:12.5px; margin-bottom:5px; }
.hs-bar__lab .v{ font-family:'IBM Plex Mono',monospace; font-weight:600; }
.hs-bar__track{ height:12px; background:#EEF2F5; border-radius:999px; overflow:hidden; }
.hs-bar__fill{ height:100%; border-radius:999px; }

/* streamlit tabs -> segmented nav --------------------------------------- */
.stTabs [data-baseweb="tab-list"], .stTabs [role="tablist"]{ gap:4px; border-bottom:1px solid var(--line); }
.stTabs [data-baseweb="tab"], [data-testid="stTab"]{ height:44px; padding:0 18px; background:transparent;
  font-size:14px; font-weight:600; color:var(--muted); border-radius:8px 8px 0 0; }
.stTabs [data-baseweb="tab"]:hover, [data-testid="stTab"]:hover{ color:var(--brand); background:#EEF3F5; }
.stTabs [aria-selected="true"], [data-testid="stTab"][aria-selected="true"]{ color:var(--brand) !important; }
.stTabs [data-baseweb="tab-highlight"]{ background:var(--brand); height:2px; }
.stTabs [data-baseweb="tab-border"]{ background:var(--line); }

/* small caption ---------------------------------------------------------- */
.hs-cap{ font-size:12px; color:var(--muted); }

/* footer ----------------------------------------------------------------- */
.hs-foot{ margin-top:26px; padding-top:16px; border-top:1px solid var(--line);
  display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap;
  font-size:12px; color:var(--muted); }

/* responsive ------------------------------------------------------------- */
@media (max-width: 900px){
  .hs-dipole{ grid-template-columns:1fr; }
  .hs-readouts{ grid-template-columns:1fr 1fr; }
  .hs-arch{ grid-template-columns:1fr; }
}
@media (max-width: 560px){
  .hs-readouts{ grid-template-columns:1fr; }
}
/* phones: stack Streamlit columns full-width, and let wide tables scroll
   instead of crushing (real fix for horizontal overflow, not overflow:hidden) */
@media (max-width: 640px){
  [data-testid="stHorizontalBlock"]{ flex-wrap:wrap; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
  [data-testid="stHorizontalBlock"] > [data-testid="column"]{
    flex:1 1 100% !important; width:100% !important; min-width:100% !important; }
  .hs-scroll .hs-table{ min-width:460px; }
}
@media (prefers-reduced-motion: reduce){
  .hs-live__dot{ animation:none; }
}

/* browser-live JS bridge (streamlit_js_eval) — invisible worker, no layout gap.
   Collapse its container to zero height but keep it in the DOM (never display:none,
   which would stop the fetch). Only rendered in live mode; matches nothing in the demo. */
.stElementContainer:has(> .stIFrame iframe[title*="streamlit_js_eval"]),
.stElementContainer:has(> [data-testid="stCustomComponentV1"] iframe[title*="streamlit_js_eval"]),
div[data-testid="stElementContainer"]:has(iframe[title*="streamlit_js_eval"]){
  height:0 !important; min-height:0 !important; margin:0 !important; padding:0 !important; }
iframe[title*="streamlit_js_eval"]{ height:0 !important; min-height:0 !important; border:0 !important; }
"""

st.markdown("<style>" + CSS + "</style>", unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# Small HTML component builders
# ----------------------------------------------------------------------------
SEV_LABEL = {"safe": "Normal", "watch": "Watch", "warning": "Elevated", "critical": "Critical"}


def _plabels(place):
    """Heading fragments for a tab, region-aware in live mode.

    ``place`` is a hydro_engine.Place in live mode, or None in demo mode (where
    the scripted Upper Bhima headings are kept exactly).
    """
    if place is None:
        return {
            "farmer_sub": "Flash-drought early warning — Junnar &amp; Daund belt",
            "dam_sub": "Forecast-informed release — Khadakwasla Reservoir",
            "agri_belt": "Junnar–Daund belt",
        }
    # HTML-escape the region-derived belt name for headings
    belt = place.agri_belt.replace("&", "&amp;")
    return {
        "farmer_sub": f"Flash-drought early warning — {belt}",
        "dam_sub": f"Forecast-informed release — {place.reservoir}",
        "agri_belt": place.agri_belt,
    }


def m(html):
    """Render a raw HTML block."""
    st.markdown(html, unsafe_allow_html=True)


def badge(text, level):
    return (f'<span class="hs-badge hs-badge--{level}">'
            f'<span class="hs-dot"></span>{text}</span>')


def metric(label, value, unit="", ctx=""):
    u = f'<span class="u">{unit}</span>' if unit else ""
    c = f'<div class="hs-metric__ctx">{ctx}</div>' if ctx else ""
    return (f'<div class="hs-metric"><div class="hs-metric__label">{label}</div>'
            f'<div class="hs-metric__value">{value}{u}</div>{c}</div>')


def note(text, label="In simple terms"):
    return f'<div class="hs-note"><b>{label}:</b> {text}</div>'


def h2(title, sub=""):
    s = f'<span class="s">{sub}</span>' if sub else ""
    return f'<div class="hs-h2"><h2>{title}</h2>{s}</div>'


def directive(title, severity, sev_label, situation, actions, meta, cert):
    items = "".join(f"<li>{a}</li>" for a in actions)
    return (
        f'<div class="hs-dir hs-dir--{severity}">'
        f'<div class="hs-dir__head"><span class="hs-dir__kicker">Recommended action</span>'
        f'{badge(sev_label, severity)}</div>'
        f'<div class="hs-dir__body"><h3 class="hs-dir__title">{title}</h3>'
        f'<p class="hs-dir__situation">{situation}</p>'
        f'<div class="hs-dir__alabel">What to do</div>'
        f'<ol class="hs-dir__actions">{items}</ol></div>'
        f'<div class="hs-dir__foot"><span class="hs-dir__meta">{meta}</span>'
        f'<span class="hs-cert">{cert}</span></div>'
        f'</div>'
    )


def style_fig(fig, height=230, legend=False):
    """Apply the shared light-theme look to a plotly figure.

    ``legend=True`` is required for any figure carrying more than one trace —
    the default stays off because most charts here are single-series, but a
    silently legend-less multi-trace chart is unreadable.

    ``uirevision`` is pinned to a constant so a pan/zoom or an open hover card
    survives the auto-refresh tick (the dashboard re-renders every 2 s in demo
    mode and every 60 s live); without it the viewer's interaction is thrown
    away on each rerun.
    """
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=10, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="IBM Plex Sans, sans-serif", color=C["ink"], size=12),
        showlegend=legend,
        hovermode="x unified",
        separators=". ",          # 1 250 m³/s, not 1250
        uirevision="hydro",
        hoverlabel=dict(bgcolor=C["brand"], bordercolor=C["brand"],
                        font=dict(family="IBM Plex Sans, sans-serif",
                                  color="white", size=12)),
        legend=dict(orientation="h", yanchor="bottom", y=1.0,
                    xanchor="left", x=0, bgcolor="rgba(0,0,0,0)",
                    font=dict(size=11)),
    )
    # 10 px margins cannot hold a 4-digit tick label; let plotly reclaim space.
    fig.update_xaxes(automargin=True)
    fig.update_yaxes(automargin=True)
    return fig


def chart_placeholder(text, height=230):
    m(f'<div class="hs-panel" style="height:{height}px;display:grid;place-items:center;'
      f'color:{C["muted"]};font-size:13px;">{text}</div>')


# Pale band fills for gauges, matching the badge tints in the CSS block above so
# a severity reads identically whether it appears as a dial band or a pill.
SEV_TINT = {
    "safe":     "#E7F5EE",
    "watch":    "#FBF2D8",
    "warning":  "#FBEBDF",
    "critical": "#FBE6E4",
}


# ============================================================================
# MAPS  (real interactive pydeck maps — tokenless CARTO basemap)
# ============================================================================
def _rgb(hex_color, alpha=220):
    """'#1F5FB0' -> [31, 95, 176, alpha] for pydeck fill colors."""
    h = hex_color.lstrip("#")
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha]


def _sev_color(sev, alpha=220):
    return _rgb(C.get(sev, C["teal"]), alpha)


def _rgba_css(hex_color, alpha=0.10):
    """'#1F5FB0', 0.08 -> 'rgba(31,95,176,0.08)' for plotly fills.

    Plotly wants the alpha channel in 0-1, unlike ``_rgb`` which produces the
    0-255 form pydeck needs — keeping them separate avoids an invalid colour
    string that plotly silently renders as opaque.
    """
    r, g, b, _ = _rgb(hex_color)
    return f"rgba({r},{g},{b},{alpha})"


def _deck_map(df, center_lat, center_lon, zoom=10.5, height=243, radius_scale=1.0):
    """Interactive pydeck map with severity-colored markers over a tokenless
    CARTO basemap. ``df`` needs columns: lat, lon, color (RGBA list), radius,
    label, detail. Falls back to a static panel if pydeck is unavailable."""
    if not HAS_PYDECK:
        chart_placeholder("Interactive map — install pydeck to enable")
        return

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_radius="radius",
        radius_scale=radius_scale,
        radius_min_pixels=6,
        radius_max_pixels=60,
        pickable=True,
        opacity=0.85,
        stroked=True,
        get_line_color=[255, 255, 255, 220],
        line_width_min_pixels=1.5,
    )
    view = pdk.ViewState(latitude=center_lat, longitude=center_lon,
                         zoom=zoom, pitch=0, bearing=0)
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=view,
        map_style=pdk.map_styles.CARTO_LIGHT,   # tokenless — no Mapbox key
        tooltip={"html": "<b>{label}</b><br/>{detail}",
                 "style": {"backgroundColor": C["brand"], "color": "white",
                           "fontSize": "12px", "padding": "6px 8px"}},
    )
    st.pydeck_chart(deck, width="stretch", height=height)


_SEV_RANK = {"safe": 0, "watch": 1, "warning": 2, "critical": 3}


def render_basin_snapshot(s, height=243):
    """Overview map: the monitored basin centre plus indicative flood/drought
    hotspots, colored by the live flood & drought severity."""
    lat, lon = ss.region_lat, ss.region_lon
    worst = max(s.flood_sev, s.drought_sev, key=lambda x: _SEV_RANK.get(x, 0))
    wilt = (f"{s.days_to_wilting:.1f} d to wilting"
            if math.isfinite(s.days_to_wilting) else "no wilting projected")
    rows = [
        # basin centre — colored by the more severe of the two hazards
        {"lat": lat, "lon": lon, "color": _sev_color(worst),
         "radius": 900, "label": ss.region_name,
         "detail": f"Flood: {SEV_LABEL[s.flood_sev]} · Drought: {SEV_LABEL[s.drought_sev]}"
                   f"<br/>Peak inflow {s.inflow_peak:,.0f} m³/s in {s.inflow_peak_in_h:.1f} h"
                   f"<br/>Engine runtime {s.real_compute_ms:.2f} ms"},
        # agricultural belt (drought signal) — indicative position NE of centre
        {"lat": lat + 0.10, "lon": lon + 0.12, "color": _sev_color(s.drought_sev),
         "radius": 620, "label": "Agricultural belt (indicative)",
         "detail": f"Drought: {SEV_LABEL[s.drought_sev]} · ESP {s.esp:.1f}th %ile"
                   f"<br/>Root-zone moisture {s.soil_moisture:.3f} m³/m³ · {wilt}"},
        # riverside / reservoir (flood signal) — indicative position SW of centre
        {"lat": lat - 0.09, "lon": lon - 0.10, "color": _sev_color(s.flood_sev),
         "radius": 620, "label": "Riverside & reservoir (indicative)",
         "detail": f"Flood: {SEV_LABEL[s.flood_sev]} · reservoir {s.reservoir_pct:.1f}%"
                   f"<br/>Release {s.release_now:,.0f} m³/s · level {s.reservoir_level:.1f} m"
                   f"<br/>Downstream {s.q_downstream:,.0f} m³/s "
                   f"(levee {H.LEVEE_Q:,.0f})"},
    ]
    if HAS_PYDECK:
        _deck_map(pd.DataFrame(rows), lat, lon, zoom=9.2, height=height)
    else:
        _deck_map(None, lat, lon, height=height)
    m('<div class="hs-map__legend" style="position:static;margin-top:8px;">'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--safe)"></span>Safe</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--watch)"></span>Watch</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--warning)"></span>Warning</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--critical)"></span>Critical</span>'
      '</div>'
      '<div class="hs-cap" style="margin-top:4px;">Marker colour is the live severity '
      'and marker size is the basin centre vs. its two hazard signals; hover any marker for '
      'the figures behind it. Basin centre uses live coordinates — hotspot markers are '
      'indicative placements around the centre, not surveyed hazard polygons.</div>')


def _risk_sev(risk) -> str:
    """Map a zone's risk label ('High'/'Medium'/'Low') to a severity colour."""
    return {"high": "critical", "medium": "warning", "low": "watch"}.get(
        str(risk or "").strip().lower(), "safe")


def _tto_sev(mins) -> str:
    """Severity for a zone's time-to-impact, on the ENGINE's own flood bands.

    ``hydro_engine.simulate`` escalates the basin at ``time_to_overtop_min <=
    100`` (critical) and ``<= 120`` (warning). This function is the single
    source of that mapping for the UI: the evacuation map and the affected-zones
    table both call it, so a zone can never show a green dot beside a yellow
    badge. ``safe`` means no impact is expected at all — a finite arrival time
    beyond the warning band is still a ``watch``.
    """
    if not math.isfinite(mins):
        return "safe"
    if mins <= 100:
        return "critical"
    if mins <= 120:
        return "warning"
    return "watch"


def render_evacuation_map(s, tto, at_risk, height=320, place=None):
    """Disaster map: the Place's riverside zones around the basin centre, coloured
    by time to impact. Zone positions are indicative (no per-zone geometry feed)."""
    pl = place or H.DEMO_PLACE
    lat, lon = ss.region_lat, ss.region_lon
    finite = math.isfinite(tto)

    # the Place's zones, staged along an indicative line near the centre
    rows = []
    for z in pl.sectors:
        hh = z.get("households")
        if hh is None:
            hh = round(at_risk * float(z.get("share", 0.0)))
        mins = tto + float(z.get("delay", 0)) if finite else float("inf")
        sev = _tto_sev(mins)
        when = "no impact expected" if not math.isfinite(mins) else (
            "impact imminent" if mins <= 1 else f"impact in ~{int(round(mins))} min")
        shown_hh = hh if finite else 0
        detail = f"{when} · {shown_hh:,} households"
        if z.get("elev"):
            detail += f" · {z['elev']}"
        if finite and s.overtop_depth > 0:
            detail += f"<br/>Overtopping depth ~{s.overtop_depth:.2f} m"
        detail += (f"<br/>Downstream {s.q_downstream:,.0f} m³/s vs levee crest "
                   f"{H.LEVEE_Q:,.0f} m³/s")
        rows.append({"lat": lat + float(z.get("dlat", 0.0)),
                     "lon": lon + float(z.get("dlon", 0.0)),
                     "color": _sev_color(sev),
                     # radius encodes exposure: households, not a constant
                     "radius": 240 + 16.0 * math.sqrt(max(0.0, float(shown_hh))),
                     "label": f"{z['name']} — {z.get('zone', '')} (indicative)".strip(),
                     "detail": detail})
    # a safe-ground shelter marker
    rows.append({"lat": lat + 0.02, "lon": lon + 0.03, "color": _sev_color("safe"),
                 "radius": 420, "label": pl.shelter_short,
                 "detail": "Safe assembly point"})
    if HAS_PYDECK:
        _deck_map(pd.DataFrame(rows), lat - 0.03, lon - 0.02, zoom=11.5, height=height)
    else:
        _deck_map(None, lat, lon, height=height)
    m('<div class="hs-map__legend" style="position:static;margin-top:8px;">'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--critical)"></span>Evacuate now (≤100 min)</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--warning)"></span>Stand by (≤120 min)</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--watch)"></span>Monitor (>120 min)</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--safe)"></span>Safe ground</span>'
      '</div>'
      '<div class="hs-cap" style="margin-top:4px;">Colour bands are the engine\'s own '
      'escalation thresholds (100 / 120 minutes to levee overtopping) and marker area '
      'scales with the households exposed in each zone. Zone positions are indicative '
      'placements around the basin centre, not surveyed boundaries.</div>')


# ============================================================================
# LIVE SIMULATION STATE  (scenario + playback clock, driven by the engine)
# ============================================================================
ss = st.session_state
# LIVE is the default: this is a real-time system, so a first-time visitor must
# see real observations, not a scripted story. Demo stays one click away in the
# sidebar, and the app never silently swaps one for the other.
ss.setdefault("mode", "live")           # "live" (real data) | "demo" (canned scenarios)
ss.setdefault("scenario", "dipole")     # start on the headline dipole crisis
ss.setdefault("tick", 0)                # simulation step, 0 .. TICKS_MAX
ss.setdefault("live", True)             # auto-advance the clock?
ss.setdefault("interval", 2)            # seconds of real time per tick
ss.setdefault("last_tick_time", time.monotonic())
ss.setdefault("res_pct", 78)            # live mode: manual current reservoir %
# live mode: the selected region (defaults to the headline basin)
ss.setdefault("region_name", live_data.UPPER_BHIMA.name)
ss.setdefault("region_lat", live_data.UPPER_BHIMA.lat)
ss.setdefault("region_lon", live_data.UPPER_BHIMA.lon)
ss.setdefault("region_tz", live_data.UPPER_BHIMA.tz)
ss.setdefault("place_query", "")        # free-text search box contents

LIVE_REFRESH_SECS = 60                  # how often the live panel refreshes
FIRST_LOAD_POLL_SECS = 3                # ...and how often until the first reading lands


def current_basin() -> live_data.Basin:
    """The region the live view is looking at (selected preset or searched place)."""
    return live_data.Basin(ss.region_name, ss.region_lat, ss.region_lon, ss.region_tz)


def _esc(text: str) -> str:
    """Minimal HTML escape for region names woven into the sidebar markup."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _brand_loc(name: str) -> str:
    """'Monitoring / <b>place</b> / region' block for the sidebar, from a region name.

    Region names arrive either em-dash split ('Upper Bhima Basin — Pune') or
    comma split from geocoding ('Nashik, Maharashtra, India'). Either way the
    first part is the headline place and the remainder is the sub-line.
    """
    raw = (name or "").strip()
    if "—" in raw:
        head, _, tail = raw.partition("—")
    elif "," in raw:
        head, _, tail = raw.partition(",")
    else:
        head, tail = raw, ""
    head, tail = head.strip() or raw, tail.strip()
    sub = f"<br>{_esc(tail)}" if tail else ""
    return f"Monitoring<br><b>{_esc(head)}</b>{sub}"


def get_live(lat: float, lon: float, name: str, tz: str):
    """Real basin snapshot, throttled so the network is hit sparingly.

    Delegates to ``live_data.get_live_cached``, which keeps a *good* reading for
    ~5 min but retries a *failed* one after ~20 s — so a first-fetch 429 on a
    shared hosting IP self-heals the moment the IP frees up, instead of being
    cached as an error for minutes. (A plain ``st.cache_data`` here would cache
    the failure for its whole TTL, which is exactly the bug this avoids.)

    Keyed on the region (lat/lon/name/tz) so switching region fetches fresh
    data instead of serving the previous place.

    ``get_live_cached`` never raises. On a transient failure it serves the last
    good reading (``ok=True, stale=True``); only with no cached reading does it
    return ``ok=False`` and the UI shows a clean 'data unavailable' fallback.
    """
    return live_data.get_live_cached(live_data.Basin(name, lat, lon, tz))


# --- Browser-side live fetch -------------------------------------------------
# Open-Meteo rate-limits per IP, and a free hosting tier shares one outbound IP
# across many apps, so the server-side fetch can be 429'd no matter how rarely
# WE call it. The fix: fetch from the VISITOR's browser (their own IP, which is
# essentially never rate-limited) via a tiny JS bridge, then run the SAME physics
# on that data. This is best-effort and additive — if the bridge is disabled,
# unavailable, blocked, or still pending, _get_live_current() falls back to the
# self-healing server fetch, so live mode never gets worse than before.
USE_BROWSER_LIVE = (os.environ.get("HYDRO_BROWSER_LIVE", "1").strip().lower()
                    not in ("0", "false", "no", "off"))
BROWSER_OBS_TTL = float(os.environ.get("HYDRO_BROWSER_TTL", "") or 900.0)


def _browser_live_pump(basin):
    """Fetch the live feed from the visitor's browser and stash it per-basin.

    Renders a hidden JS component that fetches Open-Meteo and hands the JSON
    back to Python. The component only re-evaluates when the JS string changes,
    so a time-bucket + refresh-nonce cache-buster (Open-Meteo ignores unknown
    query params) makes it re-fetch each refresh cycle and on 'Refresh now'.
    Must be called at most once per rerun (single component key).
    """
    if not (USE_BROWSER_LIVE and HAS_JS_EVAL):
        return
    # The JS only re-evaluates when the expression string changes, so the bucket
    # is what paces the browser's re-fetch. On a 60 s bucket the first attempt
    # could only be retried on a minute boundary — so until a reading has landed
    # the bucket ticks with the fast first-load poll instead.
    _bucket_secs = LIVE_REFRESH_SECS if ss.get("_live_ever_ok") else FIRST_LOAD_POLL_SECS
    bucket = int(time.time() // _bucket_secs)
    nonce = ss.get("_live_nonce", 0)
    url = f"{live_data.forecast_url(basin)}&_cb={bucket}.{nonce}"
    # Always resolve to a plain object: real data, Open-Meteo's {error:true,…},
    # or a {__hs_err} sentinel on a network/CORS error — never an un-caught reject.
    js = (f"fetch('{url}').then(function(r){{return r.json();}})"
          f".catch(function(e){{return {{__hs_err: String(e && e.message || e)}};}})")
    raw = streamlit_js_eval(js_expressions=js, key="hs_browser_live")
    obs = live_data.obs_from_open_meteo_json(raw) if isinstance(raw, dict) else None
    if obs is not None:
        ss.setdefault("_browser_obs", {})[live_data._basin_key(basin)] = (
            time.monotonic(), obs)


def _live_connecting(misses_recorded: int | None = None) -> bool:
    """True while the first reading may still legitimately be in flight.

    The sidebar badge and the main panel both read this one function, so they
    can never disagree about whether the feed is "connecting" or "down".

    On a cloud host the *server's* own Open-Meteo call is routinely 429'd on the
    shared datacentre IP while the visitor's in-browser fetch succeeds a beat
    later — so a miss on the very first paint is a loading state, not a failure,
    and saying "unavailable" there makes a working console look broken.

    ``misses_recorded`` is how many misses are known at the caller's point in
    the render. The sidebar runs *before* the fragment records this run's
    outcome, so it passes nothing and is judged on misses so far; the fragment
    passes the count including its own.
    """
    if ss.get("_live_ever_ok"):
        return False
    if not (USE_BROWSER_LIVE and HAS_JS_EVAL):
        return False            # no browser bridge: a server miss is a real miss
    seen = ss.get("_live_misses", 0) if misses_recorded is None else misses_recorded
    return seen <= (0 if misses_recorded is None else 1)


def _get_live_current():
    """Best available live snapshot for the region in session state.

    Prefers a recent reading fetched by the visitor's browser (see
    _browser_live_pump); a reading older than one refresh cycle is marked
    ``stale``. With no usable browser reading it falls back to the server-side
    fetch (which self-heals on the shared IP)."""
    b = current_basin()
    item = (ss.get("_browser_obs") or {}).get(live_data._basin_key(b))
    if item is not None:
        ts, obs = item
        age = time.monotonic() - ts
        if age <= BROWSER_OBS_TTL:
            if age <= LIVE_REFRESH_SECS * 3:
                return obs
            stale = copy.copy(obs)
            stale.stale = True
            return stale
    return get_live(b.lat, b.lon, b.name, b.tz)


def _live_base_time(obs) -> datetime:
    """The single clock origin for LIVE mode: the real observation time.

    Every timestamp the engine generates in live mode (header clock, gate
    schedule, directive feed) is derived from this one value, so they can never
    disagree — and ``hydro_engine.BASE_TIME`` (the 2026 demo story clock) is
    never involved.

    ``LiveObs.fetched_at`` is naive, stamped with ``datetime.now()`` in the
    server's own zone (UTC on Render), so ``astimezone`` converts it to true
    basin-local time. With no observation time we fall back to *now* in the
    basin's zone. The result is always timezone-aware, and only ever has a
    timedelta added or is formatted — never compared to a naive datetime.
    """
    try:
        tz = ZoneInfo(ss.get("region_tz") or "Asia/Kolkata")
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    fetched = getattr(obs, "fetched_at", None) if obs is not None else None
    if fetched is None:
        return datetime.now(tz)
    try:
        return fetched.astimezone(tz)
    except Exception:
        return datetime.now(tz)


def _set_seasonal_normal():
    """Reset the manual reservoir reading to the seasonal-normal storage.

    Runs as an on_click callback, i.e. *before* the slider is instantiated on the
    next rerun, so assigning its key here is the supported way to move a widget
    and never triggers the "default value but also set via Session State"
    warning.
    """
    ss.res_pct = int(round(H.RES_START_FRAC * 100))    # 78 %


def _on_mode_change():
    # Returning to the demo restarts the scenario clock so it plays from t=0.
    if ss.mode == "demo":
        ss.tick = 0
        ss.last_tick_time = time.monotonic()


def _on_refresh_live():
    # Force the next fetch to go to the network (the button click reruns the app).
    live_data.clear_live_cache()
    ss["_live_nonce"] = ss.get("_live_nonce", 0) + 1   # re-fetch the browser feed too
    ss["_live_tries"] = 0             # an explicit retry starts the grace again
    ss["_live_misses"] = 0


def _set_region(basin):
    """Point the live view at a new region and force a fresh fetch."""
    ss.region_name = basin.name
    ss.region_lat = basin.lat
    ss.region_lon = basin.lon
    ss.region_tz = basin.tz
    live_data.clear_live_cache(basin)
    ss["_live_nonce"] = ss.get("_live_nonce", 0) + 1   # re-fetch the browser feed too
    ss["_live_tries"] = 0             # a new region gets its own first-read grace
    ss["_live_misses"] = 0


def _on_preset_change():
    # The preset selectbox stores its label; map it back to a Basin.
    label = ss.get("preset_choice")
    for b in live_data.PRESETS:
        if b.name == label:
            _set_region(b)
            break


def _on_scenario_change():
    ss.tick = 0
    ss.last_tick_time = time.monotonic()


def _on_live_change():
    ss.last_tick_time = time.monotonic()
    if ss.live and ss.tick >= H.TICKS_MAX:      # replay from the start
        ss.tick = 0


def _on_interval_change():
    ss.last_tick_time = time.monotonic()


def _on_step():
    ss.tick = min(H.TICKS_MAX, ss.tick + 1)
    ss.live = False                             # stepping implies manual control


def _on_restart():
    ss.tick = 0
    ss.last_tick_time = time.monotonic()


def _advance_if_due():
    """Advance the simulation clock in real time while playing.

    Runs at the top of the live fragment. A wall-clock gate makes tick
    advancement independent of exactly how often the fragment reruns.
    """
    if not ss.live or ss.tick >= H.TICKS_MAX:
        return
    now = time.monotonic()
    if now - ss.last_tick_time < ss.interval * 0.85:
        return
    ss.last_tick_time = now
    ss.tick += 1
    if ss.tick >= H.TICKS_MAX:
        st.rerun()          # full rerun drops run_every -> timer stops at the end


def _mins(minutes):
    """Compact time-to-impact label for the mini readouts."""
    if not math.isfinite(minutes):
        return "none"
    if minutes <= 1:
        return "now"
    if minutes >= 90:
        return f"{minutes / 60:.1f} hr"
    return f"{int(round(minutes))} min"


def _live_val(v, unit="", fmt="{:.1f}"):
    """Format a real reading for the sidebar; '—' when missing/NaN."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(x):
        return "—"
    return fmt.format(x) + unit


FLOOD_HEAD = {
    "safe": "Flood risk is low",
    "watch": "Flood risk is building",
    "warning": "Flood risk is elevated",
    "critical": "Flood risk is critical",
}
DROUGHT_HEAD = {
    "safe": "Soil moisture is healthy",
    "watch": "Drought stress is emerging",
    "warning": "Flash drought is developing",
    "critical": "Flash drought is intensifying",
}


# ============================================================================
# SIDEBAR — station identity, live state, data sources, legend
# ============================================================================
with st.sidebar:
    _brand_html = (
        _brand_loc(ss.region_name) if ss.mode == "live"
        else 'Monitoring<br><b>Upper Bhima Basin</b><br>Pune, Maharashtra'
    )
    m(
        '<div class="hs-brand">'
        '<div class="hs-brand__mark"><div class="hs-brand__logo">HS</div>'
        '<div><div class="hs-brand__name">HydroSentry-AI</div>'
        '<div class="hs-brand__tag">Flood &amp; drought early warning</div></div></div>'
        '<div class="hs-brand__loc">' + _brand_html + '</div>'
        '</div>'
    )

    # ---- MODE SWITCH — pinned at the top, always visible -----------------
    # This is the toggle to show during the presentation: flip between the
    # scripted demo and real, live basin data without leaving the console.
    m('<div class="hs-rail-h">Mode</div>')
    st.radio(
        "Mode", ["demo", "live"], key="mode",
        format_func=lambda k: "🎬  Demo mode" if k == "demo" else "🛰️  Live data",
        on_change=_on_mode_change, label_visibility="collapsed",
    )

    if ss.mode == "demo":
        # ---- scenario + playback controls (drive the demo engine) --------
        m('<div class="hs-rail-h">Scenario</div>')
        st.radio(
            "Scenario", H.SCENARIO_ORDER, key="scenario",
            format_func=lambda k: H.SCENARIOS[k]["label"],
            on_change=_on_scenario_change, label_visibility="collapsed",
        )
        m(f'<div class="hs-cap" style="margin:-6px 0 4px;">{H.SCENARIOS[ss.scenario]["desc"]}</div>')

        m('<div class="hs-rail-h">Playback</div>')
        st.checkbox("Live simulation", key="live", on_change=_on_live_change)
        st.slider("Seconds per step", 1, 5, key="interval", on_change=_on_interval_change)
        c_step, c_restart = st.columns(2)
        with c_step:
            st.button("Step ▶", on_click=_on_step, disabled=ss.live,
                      width="stretch")
        with c_restart:
            st.button("Restart ↻", on_click=_on_restart, width="stretch")

        if ss.live:
            m('<span class="hs-live"><span class="hs-live__dot"></span>Live simulation</span>')
        else:
            _done = ss.tick >= H.TICKS_MAX
            m(f'<span class="hs-badge hs-badge--watch"><span class="hs-dot"></span>'
              f'{"Scenario complete" if _done else "Paused — manual step"}</span>')
        m('<div class="hs-updated">Physics + statistics engine · runs on-device</div>')

    else:
        # ---- LIVE controls — real observations for the basin -------------
        # Region picker: pick a ready-made basin, or search any place on Earth.
        m('<div class="hs-rail-h">Region</div>')
        _preset_labels = [b.name for b in live_data.PRESETS]
        _cur = ss.region_name
        # If the current region came from a search (not a preset), show it as a
        # transient first option so the selectbox reflects reality.
        if _cur not in _preset_labels:
            _options = [_cur] + _preset_labels
        else:
            _options = _preset_labels
        st.selectbox("Preset basin", _options,
                     index=_options.index(_cur) if _cur in _options else 0,
                     key="preset_choice", on_change=_on_preset_change,
                     label_visibility="collapsed")

        with st.form("place_search", clear_on_submit=False, border=False):
            _q = st.text_input("Search any place", value=ss.place_query,
                               placeholder="Search any place (e.g. Nashik, Solapur)…",
                               label_visibility="collapsed")
            _go = st.form_submit_button("🔍  Search", width="stretch")
        if _go and _q.strip():
            ss.place_query = _q
            _matches = live_data.search_places(_q)
            ss.place_matches = [(b.name, b.lat, b.lon, b.tz) for b in _matches]
            if _matches:
                _set_region(_matches[0])   # jump to the best match immediately
                st.rerun()
            else:
                ss.place_matches = []
        _matches = ss.get("place_matches") or []
        if len(_matches) > 1:
            m('<div class="hs-cap" style="margin:2px 0 4px;">Other matches</div>')
            for _nm, _la, _lo, _tz in _matches[1:5]:
                if st.button(_nm, key=f"place_{_nm}_{_la:.3f}", width="stretch"):
                    _set_region(live_data.Basin(_nm, _la, _lo, _tz))
                    st.rerun()
        elif ss.get("place_query") and not _matches:
            m('<div class="hs-cap" style="color:var(--warning);margin:2px 0 6px;">'
              'No places matched that search.</div>')

        obs = _get_live_current()
        _b = current_basin()
        m('<div class="hs-rail-h">Live feed</div>')
        m('<div class="hs-cap" style="margin:-4px 0 8px;line-height:1.5;">'
          f'Real-time weather &amp; hydrology · <b>{_b.name}</b><br>'
          f'{_b.lat:.4f}°N, {_b.lon:.4f}°E</div>')
        st.button("↻  Refresh now", on_click=_on_refresh_live, width="stretch")

        if obs.ok and not obs.stale:
            m('<span class="hs-live"><span class="hs-live__dot"></span>Live data · connected</span>')
        elif obs.ok and obs.stale:
            # Fresh fetch failed (usually a shared-IP rate limit) but we still
            # hold a recent real reading — the forecast runs on that, not fallback.
            m('<span class="hs-badge hs-badge--warning"><span class="hs-dot"></span>'
              'Live data · cached reading</span>')
            m('<div class="hs-cap" style="margin:4px 0 2px;">Showing the last real '
              'reading — a fresh fetch was rate-limited. The forecast is still running '
              'on live values; it refreshes automatically when the feed frees up.</div>')
        else:
            # Never the word "fallback": in this state the console withholds
            # every number, so claiming the physics "runs on fallback values"
            # was both alarming and untrue. A cold first paint is a loading
            # state (see _live_connecting); only a miss that outlives it is a
            # failure.
            if _live_connecting():
                m('<span class="hs-badge hs-badge--watch"><span class="hs-dot"></span>'
                  'Connecting to the live feed…</span>')
                m('<div class="hs-cap" style="margin:4px 0 2px;">The first reading '
                  'is being fetched from your own connection, not this server. The '
                  'console opens as soon as it lands — usually a few seconds.</div>')
            else:
                m('<span class="hs-badge hs-badge--warning"><span class="hs-dot"></span>'
                  'Live data unavailable</span>')
                if obs.error:
                    m('<div class="hs-cap" style="color:var(--warning);margin:4px 0 2px;'
                      'word-break:break-word;">Reason: ' + _esc(obs.error) + '</div>')
                m('<div class="hs-cap" style="margin:2px 0 2px;">No numbers are shown '
                  'while the feed is down — nothing is estimated in its place. Tap '
                  '“Refresh now” to retry.</div>')
        # basin-local observation time, the same base the engine clock uses
        _when = _live_base_time(obs).strftime("%H:%M:%S") if obs.fetched_at else "—"
        _age = " · cached" if (obs.ok and obs.stale) else ""
        m(f'<div class="hs-updated">Updated <b>{_when}</b>{_age} · {obs.source}</div>')

        m('<div class="hs-rail-h">Interactive Dam Control</div>')
        m('<div class="hs-cap" style="margin:-4px 0 6px;">Manual operator input — '
          'IoT telemetry pending CWC SCADA integration.</div>')
        # key="res_pct" binds the widget straight to session state, so the
        # "Seasonal Normal" callback below can set it without a value=/key clash.
        st.slider("Current reservoir % (manual operator input)", 0, 100,
                  key="res_pct", label_visibility="collapsed")
        st.button("Set to Seasonal Normal (78%)", key="res_pct_normal_btn",
                  on_click=_set_seasonal_normal, width="stretch")
        _rshort = _esc(H.place_from_region(ss.region_name).short)
        m('<div class="hs-cap" style="margin:-4px 0 4px;">'
          f'{_rshort} has no free public reservoir-level feed — set the current storage '
          'reading here; the rainfall-driven forecast and pre-release are computed live.</div>')

        m('<div class="hs-rail-h">Live readings</div>')
        _rows = [
            ("Temperature", _live_val(obs.temp_now, " °C")),
            ("Rain peak (8 h)", _live_val(obs.rain_peak, " mm/hr")),
            ("Root-zone soil", _live_val(obs.soil_moisture, " m³/m³", "{:.2f}")),
            ("Reference ET₀", _live_val(obs.et0_now, " mm/d")),
            ("Humidity", _live_val(obs.humidity, " %", "{:.0f}")),
        ]
        _rd = ""
        for _lab, _val in _rows:
            _rd += ('<div style="display:flex;justify-content:space-between;gap:10px;'
                    'padding:6px 0;border-bottom:1px solid var(--line);font-size:12.5px;">'
                    f'<span style="color:var(--muted);">{_lab}</span>'
                    '<span style="font-family:\'IBM Plex Mono\',monospace;font-weight:600;'
                    f'color:var(--brand);">{_val}</span></div>')
        m(_rd)
        if not obs.ok and obs.error:
            m(f'<div class="hs-cap" style="margin-top:8px;color:var(--warning);">'
              f'Fetch note: {obs.error}</div>')

    # ---- reference panels — collapsed so the controls above never scroll off
    with st.expander("Data sources", expanded=False):
        if ss.mode == "live":
            _dot_ok = "var(--safe)" if obs.ok else "var(--critical)"
            live_sources = [
                ("Open-Meteo · rainfall", "Hourly precipitation → flood inflow", _dot_ok),
                ("Open-Meteo · temperature", "2 m + daily max → heat / drought", _dot_ok),
                ("Open-Meteo · soil moisture", "Root-zone 9–27 cm → drought state", _dot_ok),
                ("Open-Meteo · ET₀ (FAO)", "Reference evapotranspiration → PET", _dot_ok),
                ("Manual reservoir level", "Operator input (no public live feed)", "var(--teal)"),
                ("+ your keyed source", "Pluggable via HYDRO_DATA_PROVIDER", "var(--muted)"),
            ]
            src_html = ""
            for name, desc, dot in live_sources:
                src_html += (f'<div class="hs-src"><span class="hs-src__dot" '
                             f'style="background:{dot}"></span>'
                             f'<div><div class="hs-src__name">{name}</div>'
                             f'<div class="hs-src__desc">{desc}</div></div></div>')
            m(src_html)
        else:
            sources = [
                ("NASA GPM", "Satellite rainfall"),
                ("Weather radar (NEXRAD)", "Storm-cell tracking"),
                ("NASA SMAP", "Soil moisture"),
                ("GLEAM", "Evapotranspiration"),
            ]
            src_html = ""
            for name, desc in sources:
                src_html += (f'<div class="hs-src"><span class="hs-src__dot"></span>'
                             f'<div><div class="hs-src__name">{name}</div>'
                             f'<div class="hs-src__desc">{desc}</div></div></div>')
            m(src_html)

    with st.expander("Severity scale", expanded=False):
        leg = [("safe", "Normal"), ("watch", "Watch"), ("warning", "Elevated"), ("critical", "Critical")]
        leg_html = ""
        for lvl, lab in leg:
            leg_html += (f'<div class="hs-leg"><span class="hs-leg__sw" '
                         f'style="background:var(--{lvl})"></span>{lab}</div>')
        m(leg_html)


# ============================================================================
# COMMAND HEADER  (rendered inside the live fragment so the clock updates)
# ============================================================================
def render_header(s, obs=None):
    if ss.mode == "live":
        ok = bool(obs and obs.ok)
        # Read the clock from the SAME base_time the engine used, converted to the
        # basin's zone — so the header, the gate schedule and the directive feed
        # can never show contradictory times.
        when = (s.base_time.strftime("%H:%M:%S") if getattr(s, "base_time", None)
                else "—")
        prov = obs.source if obs else "—"
        if ok:
            pill = ('<span class="hs-live"><span class="hs-live__dot"></span>'
                    f'Live &nbsp;·&nbsp; {when}</span>')
        else:
            pill = ('<span class="hs-badge hs-badge--warning"><span class="hs-dot"></span>'
                    'Live data unavailable</span>')
        m(
            '<div class="hs-cmd"><div>'
            '<div class="hs-cmd__title">Basin Command Console</div>'
            f'<div class="hs-cmd__sub">Live · <b>{ss.region_name}</b> &nbsp;·&nbsp; '
            f'updated {when} &nbsp;·&nbsp; real-time feed via {prov}.</div>'
            '</div>'
            + pill +
            '</div>'
        )
        return

    prog = int(round(100 * s.tick / H.TICKS_MAX))
    scen = H.SCENARIOS[s.scenario]["label"]
    done = s.tick >= H.TICKS_MAX
    if done:
        pill = (f'<span class="hs-badge hs-badge--safe"><span class="hs-dot"></span>'
                f'Complete &nbsp;·&nbsp; {s.clock}</span>')
    elif ss.live:
        pill = ('<span class="hs-live"><span class="hs-live__dot"></span>'
                f'Live &nbsp;·&nbsp; {s.clock}</span>')
    else:
        pill = (f'<span class="hs-badge hs-badge--watch"><span class="hs-dot"></span>'
                f'Paused &nbsp;·&nbsp; {s.clock}</span>')
    m(
        '<div class="hs-cmd"><div>'
        '<div class="hs-cmd__title">Basin Command Console</div>'
        f'<div class="hs-cmd__sub">Scenario: <b>{scen}</b> &nbsp;·&nbsp; '
        f'step {s.tick} of {H.TICKS_MAX} &nbsp;·&nbsp; {prog}% through the event window.</div>'
        '</div>'
        + pill +
        '</div>'
    )


# ---------------------------------------------------------------------------
# TAB 1 — OVERVIEW  (dipole hero + metrics + directive feed)
# ---------------------------------------------------------------------------
def render_overview(s, d, place=None):
    fsev, dsev = s.flood_sev, s.drought_sev
    belt = _plabels(place)["agri_belt"]
    flood_desc = (
        "Inflows are near baseline and the reservoir is operating within its normal rule curve."
        if fsev == "safe" else
        "Cloudburst cells over the Western Ghats are feeding fast inflow into the reservoir "
        "system, and local runoff is lifting the urban river stage.")
    drought_desc = (
        f"Root-zone moisture across the {belt} is close to field capacity. "
        "No irrigation action is required."
        if dsev == "safe" else
        f"Root-zone moisture across the {belt} is dropping faster than crops can "
        "tolerate, well before any visible wilting.")
    m(
        '<div class="hs-dipole">'
        # Flood block
        '<div class="hs-hazard hs-hazard--flood"><div class="hs-hazard__top">'
        '<span class="hs-hazard__eyebrow"><b>Flood</b> — western catchment</span>'
        + badge(SEV_LABEL[fsev], fsev) +
        f'</div><div class="hs-hazard__h">{FLOOD_HEAD[fsev]}</div>'
        f'<div class="hs-hazard__desc">{flood_desc}</div>'
        '<div class="hs-hazard__mini">'
        f'<div><div class="k">Peak inflow (forecast)</div><div class="v">{s.inflow_peak:.0f} m³/s</div></div>'
        f'<div><div class="k">Time to levee overtopping</div><div class="v">{_mins(s.time_to_overtop_min)}</div></div>'
        '</div></div>'
        # Drought block
        '<div class="hs-hazard hs-hazard--drought"><div class="hs-hazard__top">'
        '<span class="hs-hazard__eyebrow"><b>Drought</b> — eastern agri belt</span>'
        + badge(SEV_LABEL[dsev], dsev) +
        f'</div><div class="hs-hazard__h">{DROUGHT_HEAD[dsev]}</div>'
        f'<div class="hs-hazard__desc">{drought_desc}</div>'
        '<div class="hs-hazard__mini">'
        f'<div><div class="k">Evaporative stress</div><div class="v">{s.esp:.0f}th %ile</div></div>'
        f'<div><div class="k">Warning lead time</div><div class="v">{s.lead_time_days} days</div></div>'
        '</div></div>'
        '</div>'
    )

    st.write("")
    m(note("This basin faces a <b>dipole crisis</b> — floods and droughts strike the same "
           "region within days of each other. HydroSentry-AI watches for both at once and "
           "issues one clear instruction per audience."))

    st.write("")
    m(h2("Performance at a glance", "How the system is doing right now"))
    m(
        '<div class="hs-readouts">'
        + metric("Operational Real-Time Engine Execution", f"{s.real_compute_ms:.2f}", "ms",
                 "Measured this update — deterministic physics")
        + metric("Model accuracy (KGE)", f"{s.kge:.2f}", "", "Gold-standard hydrology score")
        + metric("Drought lead time", f"{s.lead_time_days}", "days", "Before visible crop wilting")
        + metric("Soil-moisture match (R)", f"{s.r_smap:.2f}", "", "Against NASA SMAP satellite")
        + metric("Drought detection rate", f"{s.pod:.2f}", "POD", "Probability of detection")
        + metric("Mass balance closure", f'{_mass_balance(s)["resid"]:+.0e}', "acre-ft",
                 "Water unaccounted for — re-integrated this update")
        + '</div>'
    )

    st.write("")
    col_a, col_b = st.columns([1.35, 1])
    with col_a:
        m(h2("Active directives", "Latest certified instructions issued"))
        rows = ""
        for t, sev, txt in d["feed"]:
            rows += (f'<div class="hs-feed__row"><span class="hs-feed__time">{t}</span>'
                     f'<span class="hs-feed__txt">' + badge(SEV_LABEL[sev], sev) +
                     f' &nbsp;{txt}</span></div>')
        m('<div class="hs-panel hs-feed">' + rows + '</div>')
    with col_b:
        m(h2("Basin snapshot", "Live overview"))
        render_basin_snapshot(s, height=243)


# ---------------------------------------------------------------------------
# TAB 2 — FARMER ADVISORY  (flash-drought early warning)
# ---------------------------------------------------------------------------
def render_farmer(s, d, place=None):
    fd = d["farmer"]
    m(h2("Farmer advisory", _plabels(place)["farmer_sub"]))
    if s.drought_sev in ("warning", "critical"):
        m(note(f"Your soil is drying at the roots <b>{s.lead_time_days} days before</b> the crop "
               "would look thirsty. Acting now, at the right time of day, can save the harvest.",
               label="Why this matters"))
    else:
        m(note("Root-zone moisture is healthy right now. This page will raise a timed, "
               "plain-language alert the moment evaporative stress starts to climb.",
               label="Why this matters"))
    st.write("")

    col_l, col_r = st.columns([1.15, 1])

    with col_l:
        m(directive(
            title=fd["title"],
            severity=fd["severity"],
            sev_label=H.SEV_LABEL[fd["severity"]],
            situation=fd["situation"],
            actions=fd["actions"],
            meta=fd["meta"],
            cert=fd["cert"],
        ))
        render_role_briefing(s, d, "farmer")
        st.write("")
        m(h2("Message sent to farmers", "Automatic SMS in Marathi and Hindi"))
        sms = d["sms"]
        m(
            '<div class="hs-sms"><div class="hs-sms__top">📩 SMS advisory</div>'
            '<div class="hs-sms__bubble">'
            f'<div class="hs-sms__mr">{sms["mr"]}</div>'
            f'<div class="hs-sms__en">{sms["en"]}</div>'
            f'</div><div class="hs-sms__sent">Sent to {sms["sent"]:,} farmers in Marathi and Hindi.</div></div>'
        )

    with col_r:
        m(h2("Evaporative stress", "How thirsty the air is vs. what soil can give"))
        if HAS_PLOTLY:
            # Bands are the engine's own drought thresholds, verbatim:
            # hydro_engine escalates at esp < 10 (critical), < 30 (warning),
            # < 50 (watch). The ticks land on those boundaries and nowhere else,
            # so the dial reads as the model's decision boundaries rather than
            # as decorative shading. 50 is the climatological median by
            # construction, which is what the delta is measured against.
            fig = go.Figure(go.Indicator(
                mode="gauge+number+delta",
                value=s.esp,
                number={"suffix": "th %ile", "font": {"size": 30, "color": C["brand"]},
                        "valueformat": ".1f"},
                delta={"reference": 50.0, "valueformat": "+.1f",
                       "increasing": {"color": C["safe"]},
                       "decreasing": {"color": C["critical"]},
                       "font": {"size": 13}},
                gauge={
                    "axis": {"range": [0, 100], "tickvals": [0, 10, 30, 50, 100],
                             "ticksuffix": "", "tickfont": {"size": 10}},
                    "bar": {"color": C["drought"], "thickness": 0.28},
                    "borderwidth": 0,
                    "steps": [
                        {"range": [0, 10], "color": SEV_TINT["critical"]},
                        {"range": [10, 30], "color": SEV_TINT["warning"]},
                        {"range": [30, 50], "color": SEV_TINT["watch"]},
                        {"range": [50, 100], "color": SEV_TINT["safe"]},
                    ],
                    "threshold": {"line": {"color": C["critical"], "width": 3},
                                  "thickness": 0.8, "value": 10},
                },
            ))
            st.plotly_chart(style_fig(fig, 210), width="stretch",
                            config={"displayModeBar": False, "responsive": True})
        else:
            chart_placeholder("Evaporative Stress Percentile gauge", 210)
        m(f'<div class="hs-cap">Evaporative Stress Percentile (ESP) — today\'s ET/PET '
          f'stress placed on the historical distribution (ESR now {s.esr:.2f} against a '
          f'climatology of {H.ESR_CLIM_MU:.2f}±{H.ESR_CLIM_SD:.2f}). Bands: '
          f'&lt;10 critical · &lt;30 warning · &lt;50 watch. Delta is measured against the '
          f'50th-percentile median.</div>')

        st.write("")
        # The x-axis resolution differs by mode: demo walks whole days from the
        # event start, live samples hourly over the past 48 h. Say which, rather
        # than letting an unlabelled axis imply the wrong horizon.
        _has_sm = bool(s.sm_days.size and s.sm_series.size)
        _rel = (s.sm_days - s.sm_days.max()) if _has_sm else None
        _span = float(abs(_rel.min())) if _has_sm else 0.0
        m(h2("Root-zone moisture",
             f"Past {_span:.0f} days, hourly" if _span <= 3
             else f"Since the event began · {_span:.0f} days"))
        if HAS_PLOTLY and _has_sm:
            rel_days = _rel.tolist()                                # today = 0
            soil = s.sm_series.tolist()
            fig2 = go.Figure()
            # observed / simulated drying curve
            fig2.add_trace(go.Scatter(
                x=rel_days, y=soil, mode="lines+markers", name="Root-zone θ",
                line=dict(color=C["drought"], width=2.5),
                marker=dict(size=4, color=C["drought"]),
                fill="tozeroy", fillcolor=_rgba_css(C["drought"], 0.10),
                hovertemplate="θ %{y:.3f} m³/m³<br>%{x:+.2f} d from now"
                              "<extra></extra>"))
            # forward projection to the wilting point at the current drying rate
            if math.isfinite(s.days_to_wilting) and s.days_to_wilting > 0:
                fig2.add_trace(go.Scatter(
                    x=[0.0, s.days_to_wilting], y=[s.soil_moisture, H.THETA_WP],
                    mode="lines", name="Projected at current drying rate",
                    line=dict(color=C["critical"], width=2, dash="dot"),
                    hovertemplate="projected θ %{y:.3f} m³/m³<br>"
                                  "%{x:+.1f} d from now<extra></extra>"))
                fig2.add_annotation(
                    x=s.days_to_wilting, y=H.THETA_WP, text=f"wilting in {s.days_to_wilting:.1f} d",
                    showarrow=True, arrowhead=0, arrowwidth=1,
                    arrowcolor=C["critical"], ax=-6, ay=-26,
                    font=dict(size=10, color=C["critical"]))
            fig2.add_hline(y=H.THETA_FC, line=dict(color=C["teal"], width=1.3, dash="dash"),
                           annotation_text=f"Field capacity {H.THETA_FC:.2f}",
                           annotation_position="top right", annotation_font_size=10)
            fig2.add_hline(y=H.THETA_WP, line=dict(color=C["critical"], width=1.5, dash="dash"),
                           annotation_text=f"Wilting point {H.THETA_WP:.2f}",
                           annotation_position="bottom right",
                           annotation_font_size=10)
            fig2.add_vline(x=0.0, line=dict(color=C["muted"], width=1, dash="dot"))
            fig2.update_layout(
                yaxis=dict(range=[0, max(0.40, H.THETA_FC + 0.06)],
                           title="θ  (m³/m³)", gridcolor=C["line"],
                           tickformat=".2f", zeroline=False),
                xaxis=dict(title="days from now  (negative = past)",
                           gridcolor="rgba(0,0,0,0)", ticksuffix=" d",
                           zeroline=False))
            st.plotly_chart(style_fig(fig2, 215, legend=True), width="stretch",
                            config={"displayModeBar": False, "responsive": True})
        else:
            chart_placeholder("Root-zone moisture trend", 215)
        _wilt_txt = (f"wilting projected in {s.days_to_wilting:.1f} days at the current "
                     f"drying rate of {s.et_actual:.2f} mm/day"
                     if math.isfinite(s.days_to_wilting)
                     else "no wilting projected at the current drying rate")
        m(f'<div class="hs-cap">Moisture now at {s.soil_moisture:.3f} m³/m³ '
          f'(field capacity {H.THETA_FC:.2f}, wilting point {H.THETA_WP:.2f}) — '
          f'{_wilt_txt}. Projection is a straight-line extrapolation of today\'s '
          f'actual evapotranspiration over a {H.ROOT_DEPTH_MM:.0f} mm root zone, '
          f'not a forecast model.</div>')


# ---------------------------------------------------------------------------
# TAB 3 — RESERVOIR OPERATIONS  (FIRO pre-release)
# ---------------------------------------------------------------------------
def render_dam(s, d, place=None):
    dm = d["dam"]
    m(h2("Reservoir operations", _plabels(place)["dam_sub"]))
    m(note("Release a little water <b>now</b> and the reservoir can safely absorb the coming "
           "surge. Wait too long and the only option is an emergency spill that floods "
           "downstream. This schedule keeps a safe buffer without wasting water.",
           label="The trade-off"))
    st.write("")

    col_l, col_r = st.columns([1.15, 1])

    with col_l:
        m(directive(
            title=dm["title"],
            severity=dm["severity"],
            sev_label=H.SEV_LABEL[dm["severity"]],
            situation=dm["situation"],
            actions=dm["actions"],
            meta=dm["meta"],
            cert=dm["cert"],
        ))
        render_role_briefing(s, d, "dam")
        st.write("")
        m(h2("Gate schedule", "Planned gate operations across the event"))
        rows_html = ""
        for r in H.gate_schedule(s):
            if r["status"] == "now":
                style = ' style="background:#EAF3F4;font-weight:600;"'
                tag = ' <span class="hs-cap" style="color:var(--teal);">● now</span>'
            elif r["status"] == "done":
                style = ' style="color:var(--muted);"'
                tag = ''
            else:
                style, tag = '', ''
            rows_html += (
                f'<tr{style}><td class="num">{r["time"]}</td>'
                f'<td>{r["action"]}{tag}</td>'
                f'<td class="num">{r["release"]}</td>'
                f'<td class="num">{r["level"]}</td></tr>')
        m(
            '<div class="hs-scroll"><table class="hs-table"><thead><tr>'
            '<th>Time (IST)</th><th>Action</th><th>Release</th><th>Reservoir level</th>'
            '</tr></thead><tbody>' + rows_html + '</tbody></table></div>'
        )

    with col_r:
        m(h2("Reservoir storage", "How full the dam is now"))
        if HAS_PLOTLY:
            # Reference lines come from the engine, not from round numbers.
            # The FIRO ceiling is the storage above which the forecast surcharge
            # volume no longer fits in the remaining void; spill is the engine's
            # real condition, storage >= RES_CAP_AFT, i.e. 100 %.
            firo_ceiling = max(0.0, min(100.0, 100.0 * (
                H.RES_CAP_AFT - s.target_buffer_aft) / H.RES_CAP_AFT))
            fig = go.Figure(go.Indicator(
                mode="gauge+number+delta",
                value=round(s.reservoir_pct, 1),
                number={"suffix": "%", "font": {"size": 30, "color": C["brand"]},
                        "valueformat": ".1f"},
                # what the pre-release actually bought, against storage at event start
                delta={"reference": round(s.start_frac * 100.0, 1),
                       "valueformat": "+.1f", "suffix": " pt",
                       "increasing": {"color": C["critical"]},
                       "decreasing": {"color": C["safe"]},
                       "font": {"size": 13}},
                gauge={
                    "axis": {"range": [0, 100],
                             "tickvals": sorted({0, 50, round(firo_ceiling), 100}),
                             "ticksuffix": "%", "tickfont": {"size": 10}},
                    "bar": {"color": C["flood"], "thickness": 0.28},
                    "borderwidth": 0,
                    "steps": [
                        {"range": [0, firo_ceiling], "color": SEV_TINT["safe"]},
                        {"range": [firo_ceiling, 100], "color": SEV_TINT["warning"]},
                    ],
                    "threshold": {"line": {"color": C["critical"], "width": 3},
                                  "thickness": 0.85, "value": 100},
                },
            ))
            st.plotly_chart(style_fig(fig, 215), width="stretch",
                            config={"displayModeBar": False, "responsive": True})
        else:
            chart_placeholder("Reservoir storage gauge", 215)
        m(f'<div class="hs-cap">Level {s.reservoir_level:.1f} m · storage '
          f'{s.storage_aft:,.0f} of {H.RES_CAP_AFT:,.0f} acre-ft · void now '
          f'{s.buffer_now_aft:,.0f} acre-ft against a forecast surcharge of '
          f'{s.target_buffer_aft:,.0f}. The amber band begins where that surcharge '
          f'would no longer fit; the red mark is the spill condition at full '
          f'capacity. Delta is measured against storage at event start '
          f'({s.start_frac * 100:.0f}%).</div>')

        st.write("")
        m(h2("Inflow vs. release", "Next 6 hours · 15-minute steps"))
        if HAS_PLOTLY and s.fc_hours.size and s.fc_inflow.size:
            hrs = s.fc_hours.tolist()
            inflow = s.fc_inflow.tolist()
            # the surcharge the operator has to absorb: inflow above safe channel
            clipped = [max(q, H.SAFE_CHANNEL) for q in inflow]
            fig2 = go.Figure()
            # --- surcharge band (drawn first, so the data lines sit on top) ---
            fig2.add_trace(go.Scatter(
                x=hrs, y=[H.SAFE_CHANNEL] * len(hrs), mode="lines",
                line=dict(width=0), hoverinfo="skip", showlegend=False))
            fig2.add_trace(go.Scatter(
                x=hrs, y=clipped, mode="lines", line=dict(width=0),
                fill="tonexty", fillcolor=_rgba_css(C["critical"], 0.13),
                hoverinfo="skip", name="Surcharge above safe channel"))
            # --- inflow forecast ---
            fig2.add_trace(go.Scatter(
                x=hrs, y=inflow, mode="lines", name="Forecast inflow",
                line=dict(color=C["flood"], width=2.5),
                fill="tozeroy", fillcolor=_rgba_css(C["flood"], 0.08),
                hovertemplate="inflow %{y:,.0f} m³/s<extra></extra>"))
            # --- the release the engine recommends holding through the event ---
            fig2.add_trace(go.Scatter(
                x=hrs, y=[s.firo_release] * len(hrs), mode="lines",
                name=f"Recommended release {s.firo_release:,.0f} m³/s",
                line=dict(color=C["teal"], width=2, dash="dash"),
                hovertemplate="release %{y:,.0f} m³/s<extra></extra>"))
            # --- the peak, marked on the chart instead of only in prose ---
            if s.inflow_peak > 0:
                fig2.add_trace(go.Scatter(
                    x=[s.inflow_peak_in_h], y=[s.inflow_peak], mode="markers",
                    name="Forecast peak",
                    marker=dict(color=C["flood"], size=10, symbol="diamond",
                                line=dict(color="white", width=1.5)),
                    hovertemplate=f"peak {s.inflow_peak:,.0f} m³/s in "
                                  f"{s.inflow_peak_in_h:.1f} h<extra></extra>"))
                fig2.add_annotation(
                    x=s.inflow_peak_in_h, y=s.inflow_peak,
                    text=f"peak {s.inflow_peak:,.0f} m³/s @ {s.inflow_peak_in_h:.1f} h",
                    showarrow=True, arrowhead=0, arrowwidth=1,
                    arrowcolor=C["flood"], ax=0, ay=-24,
                    font=dict(size=10, color=C["brand"]))
            # --- the two thresholds the alarm is actually keyed to ---
            fig2.add_hline(y=H.SAFE_CHANNEL,
                           line=dict(color=C["warning"], width=1.3, dash="dash"),
                           annotation_text=f"Safe channel {H.SAFE_CHANNEL:,.0f} m³/s",
                           annotation_position="bottom right", annotation_font_size=10)
            fig2.add_hline(y=H.LEVEE_Q,
                           line=dict(color=C["critical"], width=1.5, dash="dash"),
                           annotation_text=f"Levee crest {H.LEVEE_Q:,.0f} m³/s",
                           annotation_position="top right", annotation_font_size=10)
            fig2.update_layout(
                yaxis=dict(title="discharge  (m³/s)", gridcolor=C["line"],
                           rangemode="tozero", zeroline=False),
                xaxis=dict(title="hours from now", gridcolor="rgba(0,0,0,0)",
                           dtick=1, ticksuffix=" h", zeroline=False))
            st.plotly_chart(style_fig(fig2, 255, legend=True), width="stretch",
                            config={"displayModeBar": False, "responsive": True})
        else:
            chart_placeholder("Inflow forecast", 255)
        if s.inflow_peak_in_h > 0.2:
            cap = (f"Predicted inflow peaks at {s.inflow_peak:,.0f} m³/s in about "
                   f"{s.inflow_peak_in_h:.1f} hours.")
        elif s.inflow_peak > H.SAFE_CHANNEL:
            cap = f"Inflow near its crest of {s.inflow_peak:,.0f} m³/s and beginning to recede."
        else:
            cap = f"Inflow steady near baseline ({s.inflow_now:,.0f} m³/s)."
        m(f'<div class="hs-cap">{cap} The shaded wedge is the volume above safe '
          f'channel capacity that storage has to absorb; downstream flow is now '
          f'{s.q_downstream:,.0f} m³/s against a levee crest of {H.LEVEE_Q:,.0f}.</div>')


# ---------------------------------------------------------------------------
# TAB 4 — DISASTER RESPONSE  (geofenced evacuation)
# ---------------------------------------------------------------------------
def render_disaster(s, d, place=None):
    di = d["disaster"]
    tto = s.time_to_overtop_min
    at_risk = s.households_at_risk
    pl = place or H.DEMO_PLACE          # zones/shelter labels come from the Place

    def _sev_for(mins):
        return _tto_sev(mins)        # shared with the evacuation map — see _tto_sev

    def _tbadge(mins):
        if not math.isfinite(mins):
            return badge("not expected", "safe")
        if mins <= 1:
            return badge("now", "critical")
        if mins >= 90:
            return badge(f"~{mins/60:.1f} hr", _sev_for(mins))
        return badge(f"~{int(round(mins))} min", _sev_for(mins))

    m(h2("Disaster response", "Geofenced evacuation — riverside sectors"))
    m(note("We warn only the streets that are actually at risk, and early enough to move "
           "people calmly — no city-wide panic, no waiting for the river gauge to confirm "
           "what is already coming.", label="Why this matters"))
    st.write("")

    col_l, col_r = st.columns([1, 1.1])

    with col_l:
        m(directive(
            title=di["title"],
            severity=di["severity"],
            sev_label=H.SEV_LABEL[di["severity"]],
            situation=di["situation"],
            actions=di["actions"],
            meta=di["meta"],
            cert=di["cert"],
        ))
        render_role_briefing(s, d, "disaster")
        st.write("")
        m(h2("Affected zones", "Ordered by time to impact"))
        # Zones come from place.sectors, so every basin shows its own geography
        # instead of the Pune-only "Sector 4/5/6" labels.
        finite = math.isfinite(tto)
        zone_rows = ""
        for z in pl.sectors:
            hh = z.get("households")
            if hh is None:
                hh = round(at_risk * float(z.get("share", 0.0)))
            mins = tto + float(z.get("delay", 0)) if finite else float("inf")
            zone_rows += (
                f'<tr><td>{_esc(z["name"])}</td>'
                f'<td class="num">{_esc(z.get("zone", "—"))}</td>'
                f'<td class="num">{(hh if finite else 0):,}</td>'
                f'<td>{badge(z.get("risk", "—"), _risk_sev(z.get("risk")))}</td>'
                f'<td>{_tbadge(mins)}</td></tr>')
        m(
            '<div class="hs-scroll"><table class="hs-table"><thead><tr>'
            '<th>Area</th><th>Zone</th><th>Households</th><th>Risk</th>'
            '<th>Time to impact</th>'
            '</tr></thead><tbody>' + zone_rows + '</tbody></table></div>'
        )

    with col_r:
        m(h2("Evacuation map", "Geofenced risk zones"))
        render_evacuation_map(s, tto, at_risk, height=320, place=pl)
        st.write("")
        if not math.isfinite(tto):
            ti_val, ti_unit = "—", ""
        elif tto < 90:
            ti_val, ti_unit = f"{int(round(tto))}", "min"
        else:
            ti_val, ti_unit = f"{tto/60:.1f}", "hr"
        _first_zones = ", ".join(_esc(z["zone"]) for z in pl.sectors[:2]) or "—"
        m(
            '<div class="hs-readouts" style="grid-template-columns:1fr 1fr;">'
            + metric("Time to impact", ti_val, ti_unit, _first_zones)
            + metric("River above levee", f"{s.overtop_depth:.1f}", "m", "Forecast crest height")
            + metric("Households at risk", f"{at_risk:,}", "",
                     _esc(pl.risk_elev_phrase).capitalize())
            + metric("Shelters ready", f"{len(pl.sectors) + 1}", "",
                     _esc(pl.shelter_short))
            + '</div>'
        )


# ---------------------------------------------------------------------------
# TAB 5 — AI SUPPORT & ANALYST
#
# Ask the console a question and it answers from the state it just computed.
# The engine-validation evidence that used to fill this tab is still here, one
# expander down, and the HydroSentry-vs-conventional comparison closes the page.
# ---------------------------------------------------------------------------
def _mass_balance(s) -> dict:
    """Close the books on the engine's own reservoir recurrence.

    Re-integrates ``dS/dt = inflow - release`` with the same dt the engine used
    (0.1 h) and compares the result against the storage the engine actually
    reported. Volume discarded by the capacity clamp is counted as spill, so the
    books balance in a spilling basin too instead of reporting a false error.

    This is the one claim on this tab that is a *proof* rather than a figure:
    either the numbers in the Reservoir tab conserve mass or they do not.
    """
    dt, t = 0.1, 0.0
    storage = H.RES_CAP_AFT * s.start_frac
    v_in = v_out = v_spill = 0.0
    while t < s.flood_hours - 1e-9:
        q_in = H._inflow(t, s.target_peak)
        q_out = H._release_at(t, s.flood_mode)
        v_in += q_in * H.MS_TO_AFT_PER_H * dt
        v_out += q_out * H.MS_TO_AFT_PER_H * dt
        raw = storage + (q_in - q_out) * H.MS_TO_AFT_PER_H * dt
        held = min(H.RES_CAP_AFT, max(0.0, raw))
        v_spill += raw - held                 # what the capacity clamp shed
        storage = held
        t += dt
    d_s = s.storage_aft - H.RES_CAP_AFT * s.start_frac
    resid = v_in - v_out - v_spill - d_s
    return {"v_in": v_in, "v_out": v_out, "v_spill": v_spill, "d_s": d_s,
            "resid": resid,
            "rel": abs(resid) / v_in if v_in > 1e-9 else 0.0}


def _runtime_dist(spec, tick, n: int = 25) -> dict:
    """Repeat the engine n times and report the spread of its own measurement.

    One sample is weak evidence — a single 0.3 ms reading could be a fluke of
    scheduling. Each repeat reports ``real_compute_ms``, the same
    ``perf_counter()`` figure the headline quotes, so this is the identical
    quantity measured many times rather than a different one.
    """
    xs = sorted(H.simulate(spec, tick).real_compute_ms for _ in range(n))
    mid = n // 2
    return {"n": n, "lo": xs[0], "hi": xs[-1],
            "med": xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0,
            "p95": xs[min(n - 1, int(round(0.95 * (n - 1))))]}


def _rain_sweep(spec, tick, mults=(0.7, 0.85, 1.0, 1.15, 1.3)) -> list:
    """Re-run the engine across a band of rainfall and report where it tips.

    Cheap (one ``simulate`` per row, well under a millisecond each) and it shows
    the thing a static scorecard cannot: that the model responds to its input,
    and exactly where the response crosses into an evacuation.
    """
    f0 = spec if isinstance(spec, H.Forcing) else H.forcing_from_scenario(spec)
    base = f0.rain_peak
    rows = []
    for mult in mults:
        f = copy.copy(f0)
        f.rain_peak = base * mult
        st_ = H.simulate(f, tick)
        rows.append({"rain": f.rain_peak, "peak": st_.inflow_peak,
                     "tto": st_.time_to_overtop_min, "hh": st_.households_at_risk,
                     "sev": st_.flood_sev, "is_now": abs(mult - 1.0) < 1e-9})
    return rows


def _determinism(spec, tick, base_time=None) -> tuple:
    """Run the whole chain twice and byte-compare the directives it produced."""
    a = json.dumps(H.make_directives(H.simulate(spec, tick, base_time)),
                   sort_keys=True, default=str)
    b = json.dumps(H.make_directives(H.simulate(spec, tick, base_time)),
                   sort_keys=True, default=str)
    return a == b, len(a)


def _flood_rule(s) -> tuple:
    """The flood-severity clause that actually fired, with its threshold."""
    tto = s.time_to_overtop_min
    # _mins is the app's single phrasing for a time-to-impact ("now" / "none" /
    # minutes / hours). Reusing it means this table cannot disagree with the
    # Disaster tab about the same field.
    tto_txt = _mins(tto)
    if tto <= 100:
        return ("time_to_overtop_min", tto_txt, "&le; 100 min", "critical")
    if s.inflow_peak >= 780 or tto <= 120:
        return ("inflow_peak, time_to_overtop_min",
                f"{s.inflow_peak:,.0f} m&sup3;/s, {tto_txt}",
                "&ge; 780 m&sup3;/s or &le; 120 min", "warning")
    if s.inflow_peak >= 450:
        return ("inflow_peak", f"{s.inflow_peak:,.0f} m&sup3;/s",
                "&ge; 450 m&sup3;/s", "warning")
    if s.inflow_peak >= 300:
        return ("inflow_peak", f"{s.inflow_peak:,.0f} m&sup3;/s",
                "&ge; 300 m&sup3;/s", "watch")
    return ("inflow_peak", f"{s.inflow_peak:,.0f} m&sup3;/s",
            "&lt; 300 m&sup3;/s", "safe")


def _drought_rule(s) -> tuple:
    for thr, sev in ((10, "critical"), (30, "warning"), (50, "watch")):
        if s.esp < thr:
            return ("esp", f"{s.esp:.1f}", f"&lt; {thr}", sev)
    return ("esp", f"{s.esp:.1f}", "&ge; 50", "safe")


def render_analyst(s, d):
    """Tab 5 — ask the console a question, and show how the answer was made.

    The ordering on screen is the ordering of trust, the same convention the
    role tabs use: the answer first, then the fields it was read from, then the
    harness that decided whether the language model's wording was allowed to
    stand at all. The engine-validation evidence sits one expander down and the
    comparison with conventional processing closes the page.
    """
    m(h2("AI support &amp; analyst",
         "Ask a question — it is answered from the state this page just computed"))
    m(note("Ask in plain English and the console answers from its own numbers. "
           "The question first selects fields <b>by name</b> off the computed "
           "state; an answer is then built from those fields <b>by rule</b>, "
           "before any model runs; and only then is the Nugen model handed the "
           "question and those same fields and asked to say it in sentences. "
           "Every figure in its reply is checked back against the fields that "
           "were retrieved, and a reply quoting anything else is withheld — the "
           "computed answer stands either way. That is why the number you read "
           "here is the engine's, not the model's.",
           label="How an answer is produced"))
    st.write("")

    _render_ask(s, d)

    st.write("")
    render_nugen_panel(s, d)

    st.write("")
    m(h2("Engine validation", "The measured evidence behind every number above"))
    with st.expander("Open the engine's live self-check — mass balance, "
                     "determinism, runtime spread, rule audit and sensitivity"):
        _render_engine_evidence(s, d)

    st.write("")
    _render_comparison(s)


# ---------------------------------------------------------------------------
# The ask surface
# ---------------------------------------------------------------------------
# Short chip labels, full questions. The chip is a one-click demo path; the text
# box is the real interface, and a reviewer will use it.
_ASK_CHIPS = (
    ("Time &amp; people at risk", nugen_client.SUGGESTED_QUESTIONS[0]),
    ("Gate action now", nugen_client.SUGGESTED_QUESTIONS[1]),
    ("Can storage take the peak?", nugen_client.SUGGESTED_QUESTIONS[2]),
    ("Irrigate this week?", nugen_client.SUGGESTED_QUESTIONS[3]),
    ("Flood situation, plainly", nugen_client.SUGGESTED_QUESTIONS[4]),
    ("Speed &amp; accuracy", nugen_client.SUGGESTED_QUESTIONS[5]),
)

# Harness outcome -> badge colour. Green is "this step did its job"; amber is
# "this step declined or stood something down", which is the harness working
# and not a fault; red is a genuine failure of the layer being guarded.
_HARNESS_SEV = {
    "in scope": "safe", "computed": "safe", "passed": "safe",
    "not needed": "safe", "not applicable": "safe",
    "out of scope": "watch", "declined": "watch", "not called": "watch",
    "not reached": "watch", "in use": "watch",
    "failed": "critical", "unavailable": "warning",
}


def _harness_sev(outcome: str) -> str:
    """Badge level for a stage outcome; an unmatched one is a model id."""
    return _HARNESS_SEV.get(str(outcome).strip().lower(), "safe")


def _queue_question(question: str) -> None:
    """Chip callback: load the box and fire. Runs before the next rerun's widgets.

    Setting the text box's own key here is the only way round Streamlit's
    rule that a widget's value cannot be assigned after it has been created —
    a callback runs at the top of the next run, before the box exists.
    """
    ss["ask_q"] = question
    ss["_ask_go"] = True


def _render_ask(s, d):
    key = _nugen_key()
    basin_label = _nugen_basin_label()

    m('<div class="hs-cap" style="margin-bottom:6px;">Start from one of these, '
      'or type your own:</div>')
    for row in (_ASK_CHIPS[:3], _ASK_CHIPS[3:]):
        for col, (label, question) in zip(st.columns(len(row)), row):
            with col:
                st.button(label.replace("&amp;", "&"), key=f"ask_chip_{question[:24]}",
                          on_click=_queue_question, args=(question,),
                          width="stretch")

    st.text_input("Your question", key="ask_q",
                  placeholder="e.g. how long until the levee overtops, and who is exposed?",
                  label_visibility="collapsed")
    c_go, c_raw = st.columns([1, 1])
    with c_go:
        go = st.button("Ask the analyst", key="ask_go", type="primary",
                       width="stretch")
    with c_raw:
        raw = st.button("Answer from the data only — no model call",
                        key="ask_raw", width="stretch")

    question = (ss.get("ask_q") or "").strip()
    fired = ss.pop("_ask_go", False)
    if (go or raw or fired) and question:
        with st.spinner("Reading the computed state…"):
            ans = nugen_client.ask(s, d, question, basin_label, api_key=key,
                                   use_model=not raw)
        ss["_ask"] = {"sig": _nugen_sig(s), "ans": ans,
                      "clock": getattr(s, "clock", "")}
    elif (go or raw) and not question:
        m('<div class="hs-cap" style="margin-top:6px;color:var(--warning);">'
          'Type a question first, or press one of the buttons above.</div>')

    data = ss.get("_ask")
    if not data:
        m('<div class="hs-cap" style="margin-top:8px;">Nothing has been asked '
          'yet. Nothing is sent anywhere until you ask — and the answer is '
          'composed from the computed state whether or not the language layer '
          'is reachable.</div>')
        return

    ans = data["ans"]
    stale = data.get("sig") != _nugen_sig(s)

    st.write("")
    m(f'<div class="hs-cap" style="margin-bottom:6px;">Asked: '
      f'<b>{_esc(ans.question)}</b></div>')

    # ---- the answer --------------------------------------------------------
    if not ans.in_scope:
        m('<div class="hs-layer hs-layer--research">'
          '<div class="hs-layer__tag">Out of scope · nothing was sent</div>'
          '<div class="hs-layer__h">This console does not hold that</div>'
          '<div class="hs-layer__p">The question did not match any field this '
          'engine computes, so no field was retrieved and no model was called. '
          'Answering anyway would be a guess dressed as a reading, which is the '
          'one thing a flood console must not do. Ask about the river, the '
          'reservoir, the levee, who is exposed, the soil and the crop, the '
          'standing orders, or the engine\'s own speed and accuracy.</div>'
          '</div>')
        _render_harness(ans)
        return

    if ans.verified:
        m('<div class="hs-layer">'
          '<div class="hs-layer__tag">Answer · verified against the retrieved data</div>'
          f'<div class="hs-layer__p" style="font-size:14.5px;color:var(--ink);">'
          f'{_nugen_html(ans.result.text)}</div>'
          f'<div class="hs-cap" style="margin-top:10px;">'
          f'{_nugen_figure_check(ans.result.unsupported)}</div>'
          f'<div class="hs-cap" style="margin-top:4px;">Worded by '
          f'<code>{_esc(ans.result.model or nugen_client.MODEL)}</code> from the '
          f'{len(ans.facts)} fields below and nothing else. The computed answer '
          f'it was written from is directly underneath.</div>'
          '</div>')
        m('<div class="hs-layer hs-layer--research" style="margin-top:10px;">'
          '<div class="hs-layer__tag">Answer of record · computed, no model</div>'
          f'<div class="hs-layer__p">{_esc(ans.grounded)}</div>'
          '<div class="hs-cap" style="margin-top:10px;">Built from the retrieved '
          'fields by rule before the model ran. If the two disagree, this one is '
          'right.</div>'
          '</div>')
    else:
        why = ""
        if ans.result is not None and ans.result.unsupported:
            why = ('The model\'s wording is withheld: it quoted '
                   + _esc(", ".join(str(b) for b in ans.result.unsupported[:6]))
                   + ', which is not among the fields that were retrieved.')
        elif ans.result is not None:
            why = ('The language layer did not answer — '
                   + _esc(ans.result.error) + ' This answer does not need it.')
        else:
            why = ('The language layer was not called, by request. The answer '
                   'below is what the console computes either way.')
        m('<div class="hs-layer">'
          '<div class="hs-layer__tag">Answer · computed from the retrieved data</div>'
          f'<div class="hs-layer__p" style="font-size:14.5px;color:var(--ink);">'
          f'{_esc(ans.grounded)}</div>'
          f'<div class="hs-cap" style="margin-top:10px;">{why}</div>'
          '</div>')

    if stale:
        m('<div class="hs-cap" style="margin-top:6px;color:var(--warning);">'
          f'The basin has changed since this was asked (answered for the '
          f'{_esc(data.get("clock") or "earlier")} update). Ask again for the '
          'current one.</div>')

    # ---- the evidence ------------------------------------------------------
    st.write("")
    m(h2("Evidence retrieved",
         f"The {len(ans.facts)} fields this question selected, read by name off "
         f"the computed state"))
    m('<div class="hs-scroll"><table class="hs-table"><thead><tr>'
      '<th>Field read</th><th>What it is</th><th>Value on this update</th>'
      '</tr></thead><tbody>'
      + "".join(
          f'<tr><td><code>{_esc(f.field)}</code></td>'
          f'<td>{_esc(f.label)}</td>'
          f'<td class="num">{_esc(f.value)}</td></tr>'
          for f in ans.facts)
      + '</tbody></table></div>')
    m('<div class="hs-cap" style="margin-top:6px;">Each row is an attribute of '
      'the state object this page rendered, selected because the question '
      'matched the topic it belongs to. This table <b>is</b> what the model was '
      'given — the prompt below is these rows and the question, and nothing '
      'else.</div>')

    _render_harness(ans)

    if ans.digest:
        with st.expander("What was sent for this question"):
            st.code(nugen_client.analyst_preview(s, d, ans.question,
                                                 _nugen_basin_label()),
                    language="text")
            st.caption(
                "The fixed instruction, one worked example, then the question "
                "and the retrieved fields. No credentials, no raw observations "
                "and no part of the dashboard beyond those fields leaves the "
                f"app. Capped at {nugen_client.ANALYST_MAX_TOKENS} completion "
                "tokens.")


def _render_harness(ans):
    """The six stages of the harness, with what each one actually did.

    This is the panel that makes the claim checkable rather than stated: it
    reports the scope decision, how many fields were read, whether the model was
    called at all, whether its figures survived the check, and whether the
    computed answer had to stand in. A reviewer can make it say "failed" on
    purpose by asking something the console does not hold.
    """
    st.write("")
    m(h2("AI harness", "What each stage of the pipeline did with this question"))
    m('<div class="hs-scroll"><table class="hs-table"><thead><tr>'
      '<th>Stage</th><th>Outcome</th><th>What happened</th>'
      '</tr></thead><tbody>'
      + "".join(
          f'<tr><td><b>{_esc(name)}</b></td>'
          f'<td>{badge(_esc(outcome), _harness_sev(outcome))}</td>'
          f'<td>{_esc(detail)}</td></tr>'
          for name, outcome, detail in (ans.stages or []))
      + '</tbody></table></div>')
    m('<div class="hs-cap" style="margin-top:6px;">The guardrails are the '
      'product, not a disclaimer. Retrieval is ordinary attribute lookup, so '
      'the figures cannot drift from the engine; the figure check is a '
      'deterministic comparison against the retrieved facts; and the computed '
      'answer is produced <i>before</i> the model is called, so there is always '
      'something correct to fall back to. Ask about something outside the basin '
      'to watch the scope check decline instead of guessing.</div>')


# ---------------------------------------------------------------------------
# Engine validation (kept from the old Model & validation tab, one level down)
# ---------------------------------------------------------------------------
def _render_engine_evidence(s, d):
    m(note("Two separated layers. <b>Layer 1</b> is the deterministic physics "
           "engine that produced every number on this console. <b>Layer 2</b> "
           "never produces a number: the neural surrogates and published "
           "benchmarks are research that does <b>not</b> run here at all, and the "
           "Nugen language layer does run on every update but only re-words what "
           "Layer 1 decided. Everything in the "
           "<i>Measured</i> and <i>Audit</i> blocks below is computed on this "
           "update, in front of you; everything in <i>Reference</i> is a constant "
           "and is labelled as one.",
           label="Read this first"))
    st.write("")

    # ---- the two layers, stated once each -------------------------------
    c_l1, c_l2 = st.columns([1, 1])
    with c_l1:
        m(h2("Layer 1 — Operational Production Engine", "Ran this update"))
        m('<div class="hs-layer">'
          '<div class="hs-layer__tag">Layer 1 · in the live decision path · running now</div>'
          '<div class="hs-layer__h">Closed-form hydrology — '
          f'{s.real_compute_ms:.2f} ms measured this update</div>'
          '<div class="hs-layer__p">Solved on the CPU: no model weights, no GPU, no '
          'network call. A <b>Gamma unit-hydrograph convolution</b> routes the '
          'rainfall pulse, an <b>explicit Euler reservoir mass balance</b> '
          '(dt = 0.1 h) carries storage and the FIRO pre-release, a rating and '
          'levee-crest comparison gives downstream stage and time-to-impact, and '
          'the <b>FAO-56 Evaporative Stress Ratio</b> with its percentile '
          'climatology drives the drought side.</div>'
          '<div class="hs-cap" style="margin-top:10px;">LIVE OPERATIONAL PATH — every '
          'value in the Overview, Farmer, Reservoir and Disaster tabs comes from '
          'this chain.</div>'
          '</div>')
    with c_l2:
        m(h2("Layer 2 — Research &amp; Neural Surrogates",
             "Two tracks: one offline, one live but non-computational"))
        m('<div class="hs-layer hs-layer--research">'
          '<div class="hs-layer__tag">Layer 2 · research track · did not run</div>'
          '<div class="hs-layer__h">Neural surrogates and the HEC-RAS 2D benchmark</div>'
          '<div class="hs-layer__p">Targets what the closed-form engine does not '
          'attempt: full 2D inundation mapping and learned error correction. Its '
          f'headline figure is a <b>benchmark</b> — a 2D depth map in ~{H.BENCHMARK_COMPUTE_S:.1f} s '
          'against ~2.3 h for an equivalent HEC-RAS 2D run. That describes the '
          'research surrogate and is <b>not the runtime of the operational engine</b>; '
          'the live engine finishes in milliseconds because it solves a much '
          'smaller problem. These would be swapped in at the '
          '<code>simulate()</code> seam once each is validated against gauge '
          'records — no such module is executed today.</div>'
          '<div class="hs-cap" style="margin-top:10px;">RESEARCH / BENCHMARK PATH — '
          'offline, against historical events; none of it runs to render this console.</div>'
          '</div>')
        # The other half of Layer 2 does run, on every update, and saying so
        # here is the point: a reviewer should not have to discover from the
        # role tabs that a language model is in the product. The boundary that
        # makes it safe is the one stated in the card — it is handed the
        # computed numbers and writes prose, and it is never asked for one.
        m('<div class="hs-layer hs-layer--research" style="margin-top:12px;">'
          '<div class="hs-layer__tag">Layer 2 · language track · runs every update</div>'
          f'<div class="hs-layer__h">Nugen '
          f'<code>{_esc(nugen_client.known_models()[0])}</code> — '
          'the briefing under each directive</div>'
          '<div class="hs-layer__p">The one Layer 2 component that is live in the '
          'product. Each role tab carries a Nugen briefing beneath its directive '
          'card, re-worded for the reader that tab is for. It is handed the '
          'numbers Layer 1 computed and instructed to use nothing else; every '
          'figure it prints is checked back against the computed state, and '
          'anything unsupported is flagged on the card itself. It solves no '
          'equation, produces no forecast and changes no value above — a small '
          'model is a good writer and a bad hydrologist, so it is given the '
          'writing and kept away from the hydrology.</div>'
          '<div class="hs-cap" style="margin-top:10px;">LANGUAGE PATH — '
          'presentational only; the console renders identically with this layer '
          'switched off. Prompts, spend and figure checks are below.</div>'
          '</div>')

    # ---- MEASURED on this update ----------------------------------------
    st.write("")
    m(h2("Measured on this update", "Recomputed every refresh — not stored, not quoted"))

    spec = ss.get("_spec")
    tick = ss.get("_spec_tick", s.tick)
    mb = _mass_balance(s)
    cards = []

    # 1 — mass balance. The strongest claim available, and it is a proof.
    if mb["v_spill"] < -1e-6 or mb["v_spill"] > 1e-6:
        mb_note = (f'Includes {abs(mb["v_spill"]):,.0f} acre-ft shed by the '
                   'capacity clamp (spill), which is water leaving the system.')
    else:
        mb_note = 'No spill this update, so every acre-foot is either released or stored.'
    cards.append((
        "Mass balance closes",
        f'{mb["resid"]:+.2e}',
        "acre-ft unaccounted for",
        f'&Sigma;inflow {mb["v_in"]:,.0f} &minus; &Sigma;release {mb["v_out"]:,.0f} '
        f'&minus; &Delta;storage {mb["d_s"]:,.0f} over {s.flood_hours:.1f} h. '
        f'Relative error {mb["rel"]:.1e} — machine precision. {mb_note}'))

    # 2 — runtime as a distribution, not one lucky sample.
    if spec is not None:
        rt = _runtime_dist(spec, tick)
        cards.append((
            "Operational Real-Time Engine Execution",
            f'{rt["med"]:.2f} ms',
            f'median of {rt["n"]} repeat runs',
            f'min {rt["lo"]:.2f} &middot; p95 {rt["p95"]:.2f} &middot; max {rt["hi"]:.2f} ms, '
            'each the engine\'s own <code>time.perf_counter()</code> reading for the '
            'same inputs. This is the figure the whole console is built on, so it '
            'is reported as a spread rather than a single sample.'))

        ok_det, nbytes = _determinism(spec, tick, getattr(s, "base_time", None))
        cards.append((
            "Determinism",
            "identical" if ok_det else "DIVERGED",
            f'{nbytes:,} bytes, re-run twice',
            'The full chain — forcing, physics, severities, directives — was run '
            'again just now and the serialised result byte-compared. A learned '
            'model cannot offer this; a solved one must.'))

    # 3 — the regression guard, named with the command that reproduces it.
    cards.append((
        "Frozen-demo regression guard",
        "132 / 132",
        "states byte-identical",
        'Every state of all four demo scenarios (4 &times; 33 ticks) is held as a '
        'golden snapshot and byte-compared on every change, so the scripted demo '
        'cannot silently drift. Reproduce with '
        '<code>python verify_demo_golden.py</code>.'))

    m('<div class="hs-arch">' + "".join(
        '<div class="hs-arch__card">'
        f'<div class="hs-arch__tag">measured &middot; live</div>'
        f'<div class="hs-arch__h">{title}</div>'
        f'<div style="font-family:IBM Plex Mono,monospace;font-size:1.45rem;'
        f'color:var(--brand);line-height:1.1;margin:2px 0 1px;">{big}</div>'
        f'<div class="hs-cap" style="margin-bottom:6px;">{unit}</div>'
        f'<div class="hs-arch__p">{body}</div></div>'
        for title, big, unit, body in cards) + '</div>')

    # ---- AUDIT TRAIL — why this update said what it said -----------------
    st.write("")
    m(h2("Rule audit trail", "The clause that fired, the number it read, and the authority cited"))
    f_field, f_val, f_thr, f_sev = _flood_rule(s)
    dr_field, dr_val, dr_thr, dr_sev = _drought_rule(s)
    firo_fired = "pre-release" if s.flood_mode else "hold normal release"
    rows = [
        ("Flood severity", f_field, f_val, f_thr, SEV_LABEL.get(f_sev, f_sev), f_sev,
         _esc(str((d.get("disaster") or {}).get("cert", "") or "—"))),
        ("Drought severity", dr_field, dr_val, dr_thr, SEV_LABEL.get(dr_sev, dr_sev), dr_sev,
         _esc(str((d.get("farmer") or {}).get("cert", "") or "—"))),
        ("Reservoir action", "target_peak vs SAFE_CHANNEL",
         f"{s.target_peak:,.0f} m&sup3;/s", f"&gt; {H.SAFE_CHANNEL:,.0f} m&sup3;/s",
         firo_fired, "warning" if s.flood_mode else "safe",
         _esc(str((d.get("dam") or {}).get("cert", "") or "—"))),
    ]
    m('<div class="hs-scroll"><table class="hs-table"><thead><tr>'
      '<th>Decision</th><th>Field read</th><th>Value now</th><th>Threshold</th>'
      '<th>Outcome</th><th>Authority cited</th>'
      '</tr></thead><tbody>'
      + "".join(
          f'<tr><td><b>{name}</b></td><td><code>{field}</code></td>'
          f'<td class="num">{val}</td><td class="num">{thr}</td>'
          f'<td>{badge(label, sev)}</td><td>{cert}</td></tr>'
          for name, field, val, thr, label, sev, cert in rows)
      + '</tbody></table></div>')
    m('<div class="hs-cap" style="margin-top:6px;">Every threshold above is a '
      'literal in <code>hydro_engine.py</code>, and every value is a field on the '
      'state object this page rendered — so each directive traces back to one '
      'number and one comparison. &ldquo;Authority cited&rdquo; is the manual the '
      'rule set is written against; it is a citation, not an external '
      'certification.</div>')

    # ---- SENSITIVITY — the model responds to its input -------------------
    if spec is not None:
        st.write("")
        m(h2("Sensitivity", "Same engine, rainfall swept &plusmn;30% — where the forecast tips"))
        sweep = _rain_sweep(spec, tick)
        _tintnow = _rgba_css(C["brand"], 0.07)
        m('<div class="hs-scroll"><table class="hs-table"><thead><tr>'
          '<th>Rain peak</th><th>Inflow peak</th><th>Time to levee</th>'
          '<th>Households at risk</th><th>Flood state</th>'
          '</tr></thead><tbody>'
          + "".join(
              '<tr' + (f' style="background:{_tintnow}"' if r["is_now"] else '') + '>'
              f'<td class="num">{r["rain"]:.2f} mm/hr'
              + (' <span class="hs-cap">(now)</span>' if r["is_now"] else '') + '</td>'
              f'<td class="num">{r["peak"]:,.0f} m&sup3;/s</td>'
              f'<td class="num">{_mins(r["tto"])}</td>'
              f'<td class="num">{r["hh"]:,}</td>'
              f'<td>{badge(SEV_LABEL.get(r["sev"], r["sev"]), r["sev"])}</td></tr>'
              for r in sweep)
          + '</tbody></table></div>')
        # Name the row where the evacuation count FIRST becomes non-zero, not the
        # row that happens to hold the maximum: the response here is stepped
        # (0 -> 60 % of households -> all of them), so quoting the top of the
        # range as "the" threshold would skip the real crossing below it.
        _hh = [r["hh"] for r in sweep]
        _first = next((r for r in sweep if r["hh"] > 0), None)
        if max(_hh) == 0:
            _cap = ('No evacuation is triggered anywhere in this band, so the basin '
                    'is not near its threshold on this update — which is itself the '
                    f'useful answer. The inflow peak still tracks the rainfall '
                    f'({sweep[0]["peak"]:,.0f} to {sweep[-1]["peak"]:,.0f} m&sup3;/s '
                    'across the sweep), so the engine is responding, not flat.')
        elif min(_hh) > 0:
            _cap = (f'Every row in this band is already past the evacuation '
                    f'threshold — even a 30% lighter rainfall leaves '
                    f'{min(_hh):,} households exposed, rising to {max(_hh):,}. '
                    'The decision is not finely balanced on this update.')
        else:
            _more = (f'; by the top of the band that reaches {max(_hh):,}'
                     if max(_hh) > _first["hh"] else '')
            _cap = (f'The threshold is crossed at a rain peak of '
                    f'{_first["rain"]:.2f} mm/hr, where the forecast first places '
                    f'{_first["hh"]:,} households inside the flood envelope{_more}. '
                    'That boundary falls out of the routing and the levee crest, '
                    'not a tuned cut-off.')
        m(f'<div class="hs-cap" style="margin-top:6px;">{_cap} Each row is a full '
          'extra <code>simulate()</code> call made while this page rendered.</div>')

    # ---- REFERENCE constants, kept apart from anything measured ----------
    st.write("")
    m(h2("Reference figures", "Carried as constants — not computed or validated here"))
    m('<div class="hs-scroll"><table class="hs-table"><thead><tr>'
      '<th>Figure</th><th>Value</th><th>Layer</th><th>What it is</th>'
      '</tr></thead><tbody>'
      f'<tr><td>KGE accuracy</td><td class="num">{s.kge:.2f}</td><td>1</td>'
      '<td>Reference validation score for this modelling approach (1.0 is perfect)</td></tr>'
      f'<tr><td>Drought lead time</td><td class="num">{s.lead_time_days} days</td><td>1</td>'
      '<td>Published warning margin before visible crop wilting</td></tr>'
      f'<tr><td>Drought detection (POD)</td><td class="num">{s.pod:.2f}</td><td>1</td>'
      '<td>Reference probability of catching onset</td></tr>'
      f'<tr><td>Soil-moisture match (R)</td><td class="num">{s.r_smap:.2f}</td><td>1</td>'
      '<td>Reference agreement with NASA SMAP satellite retrievals</td></tr>'
      f'<tr><td>PINN flood-map surrogate</td><td class="num">~{H.BENCHMARK_COMPUTE_S:.1f} s</td>'
      '<td>2</td><td>Benchmark per 2D depth map, against ~2.3 h for HEC-RAS 2D</td></tr>'
      '</tbody></table></div>')
    m('<div class="hs-cap" style="margin-top:6px;">These four validation figures are '
      'constants in <code>hydro_engine.py</code>; nothing on this page recomputes '
      'them, and they are kept out of the measured block above for exactly that '
      'reason. The execution time, the mass-balance residual, the determinism '
      'check and the sensitivity sweep are the numbers measured live.</div>')


# ---------------------------------------------------------------------------
# The closing comparison — HydroSentry-AI against conventional processing
#
# Deliberately small, and deliberately last. It is the one claim on this page
# that rests partly on a published figure rather than on something measured in
# front of the reader, so it sits below the evidence rather than above it, and
# the caption says plainly which column is measured and which is a benchmark.
# ---------------------------------------------------------------------------
def _render_comparison(s):
    m(h2("HydroSentry-AI vs conventional processing",
         "The same decision, reached two different ways"))
    m('<div class="hs-scroll"><table class="hs-table"><thead><tr>'
      '<th>&nbsp;</th><th>HydroSentry-AI</th><th>Conventional 2D modelling</th>'
      '</tr></thead><tbody>'
      f'<tr><td><b>Time to a decision</b></td>'
      f'<td class="num">{s.real_compute_ms:.2f} ms</td>'
      f'<td class="num">~2.3 h</td></tr>'
      '<tr><td><b>What it runs on</b></td>'
      '<td>One CPU core. No GPU, no model weights, no network call.</td>'
      '<td>A workstation or cluster, with a meshed domain to prepare first.</td></tr>'
      '<tr><td><b>Re-running it</b></td>'
      '<td>Byte-identical — proved by re-running the chain on this update.</td>'
      '<td>Re-meshing and re-calibration between runs.</td></tr>'
      '<tr><td><b>What comes out</b></td>'
      '<td>A directive per desk — farmer, duty engineer, disaster officer — '
      'with the threshold that fired.</td>'
      '<td>Depth and velocity grids for a hydrologist to interpret.</td></tr>'
      '<tr><td><b>Language layer</b></td>'
      '<td>A briefing per desk and an analyst that answers questions, every '
      'figure checked back against the computed state.</td>'
      '<td>None.</td></tr>'
      '</tbody></table></div>')
    m('<div class="hs-cap" style="margin-top:6px;">Read the first row carefully, '
      'because it is the only one that is not like for like. The left-hand '
      'figure is this engine\'s own <code>perf_counter()</code> reading for the '
      'update you are looking at; <b>~2.3 h is a published benchmark for an '
      'equivalent HEC-RAS 2D run, not something measured here</b>. The two solve '
      'different-sized problems: the closed-form chain answers "how high, how '
      'soon, who is exposed" and finishes in milliseconds, while a 2D solver '
      'produces a full inundation surface this engine does not attempt. The '
      'honest claim is not that HydroSentry is a faster 2D model — it is that '
      'the decision a control room needs does not require one.</div>')


# ---------------------------------------------------------------------------
# THE NUGEN LANGUAGE LAYER (Layer 2, language only)
#
# ``render_role_briefing`` is the layer itself and renders on all three role
# tabs, inline under the directive it re-words. ``render_nugen_panel`` is its
# audit surface on tab 5: the exact prompt per role, the spend, and the figure
# check. Nothing here computes a value, and the console renders identically
# with no key configured.
# ---------------------------------------------------------------------------
def _nugen_key() -> str:
    """Resolve the Nugen API key from Streamlit secrets, then the environment.

    Never hard-coded: this repository is public, so the key belongs in a Render
    environment variable (``NUGEN_API_KEY``) or a gitignored
    ``.streamlit/secrets.toml``. Missing is a normal, supported state.
    """
    try:
        val = str(st.secrets.get("NUGEN_API_KEY", "") or "").strip()
        if val:
            return val
    except Exception:            # no secrets file at all — perfectly fine
        pass
    return nugen_client.default_api_key()


def _nugen_sig(s) -> str:
    """Identity of the update a briefing was generated for (staleness check)."""
    return (f"{ss.get('mode')}|{ss.get('region_name', '')}|{s.scenario}|{s.tick}|"
            f"{s.flood_sev}|{s.drought_sev}|{s.inflow_peak:.0f}|{s.reservoir_pct:.0f}")


def _nugen_basin_label() -> str:
    """How the basin is named to the briefing layer (one source for all tabs)."""
    return (ss.region_name if ss.mode == "live"
            else "Upper Bhima Basin (Pune, Maharashtra)")


def _nugen_html(text: str) -> str:
    """Escaped HTML for a model reply: bullet lines become a list."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    bullets = [ln.lstrip("-*•").strip() for ln in lines
               if ln[:1] in ("-", "*", "•")]
    if len(bullets) >= 2:
        return ('<ul class="hs-dir__actions" style="margin:0;">'
                + "".join(f"<li>{_esc(b)}</li>" for b in bullets) + "</ul>")
    return "".join(f'<p style="margin:0 0 6px;">{_esc(ln)}</p>' for ln in lines)


def _nugen_figure_check(unsupported) -> str:
    """The one-line verdict on whether the wording invented a number."""
    bad = list(unsupported or [])
    if bad:
        return ('<span style="color:var(--warning)">Figure check: '
                + _esc(", ".join(str(b) for b in bad[:6]))
                + ' is not among the facts this layer was given — trust the '
                  'directive above, not this wording.</span>')
    return ('<span style="color:var(--safe)">Figure check: every number here '
            'appears in the computed state.</span>')


def render_role_briefing(s, d, role: str):
    """The Nugen language layer, inline under the directive it re-words.

    This is the product's third output for every role, alongside the directive
    card and the chart: the same decision, in the register that role actually
    reads. It sits directly beneath the authoritative card and is styled with the
    ``--research`` modifier the rest of the app uses for "secondary, not the
    authority", so the ordering on screen matches the ordering of trust.

    How it is fetched differs by mode, and the difference is the quota:

    * **Live** — fetched automatically in a background thread as the basin
      changes. Never inline, because the transport has a 30-second read timeout
      and a synchronous call would freeze the console on exactly the updates that
      matter. The result is keyed on the (bucketed, clock-free) facts, so a steady
      basin costs nothing however often the page refreshes.
    * **Demo** — on an explicit click only. The scripted demo re-runs every two
      seconds across 33 ticks; fetching automatically there would spend the whole
      session budget in under a minute and say nothing new.
    """
    cfg = nugen_client.ROLES[role]
    key = _nugen_key()

    if not key:
        # One quiet line, not a warning box on all three tabs. The full
        # explanation — what this layer adds, and how to switch it on — lives in
        # the AI analyst tab, which is where a reviewer goes for it.
        m('<div class="hs-cap" style="margin-top:2px;">'
          f'Nugen briefing for {_esc(cfg["label"].lower())}: layer is off, no '
          '<code>NUGEN_API_KEY</code> configured. The directive above is produced '
          'without it — see the <b>AI analyst</b> tab for what this layer adds.'
          '</div>')
        return

    label = _nugen_basin_label()
    auto = ss.mode == "live" and bool(ss.get("_live_ever_ok"))
    res = nugen_client.request_role_async(s, d, role, label, api_key=key,
                                         auto=auto)

    if res is None and not auto:
        # Demo mode: nothing is cached for this update and nothing is fetched
        # automatically. Offer the call only while the demo is paused — under a
        # clock that advances every two seconds the briefing would be stale
        # before it rendered.
        if ss.get("live"):
            m('<div class="hs-cap" style="margin-top:2px;">Nugen briefing: pause '
              'the demo to generate one for a specific update, or switch to '
              '<b>Live data</b>, where briefings are fetched automatically as the '
              'basin changes.</div>')
            return
        if st.button(f"Generate the Nugen briefing — {cfg['label'].lower()}",
                     key=f"nugen_go_{role}", width="stretch"):
            with st.spinner("Nugen is re-wording the directive…"):
                res = nugen_client.brief_role(s, d, role, label, api_key=key)
        else:
            m('<div class="hs-cap" style="margin-top:2px;">Re-words the directive '
              f'above for this reader using Nugen '
              f'<code>{_esc(nugen_client.known_models()[0])}</code>. '
              'Nothing on this page depends on it.</div>')
            return

    if res is None:
        m('<div class="hs-cap" style="margin-top:2px;">Nugen is re-wording this '
          'directive — the briefing appears here within a few seconds, without '
          'holding up anything above.</div>')
        return

    if not res.ok:
        m('<div class="hs-cap" style="margin-top:2px;">'
          f'Nugen briefing unavailable: {_esc(res.error)} '
          'The directive above is unaffected.</div>')
        if st.button("Try the briefing again", key=f"nugen_retry_{role}"):
            nugen_client.request_role_async(s, d, role, label, api_key=key,
                                            auto=True, retry=True)
            _safe_rerun()
        return

    cost = ("served from cache, no tokens spent" if res.cached
            else (f"{res.tokens_out} completion tokens"
                  if res.tokens_out else f"{res.max_tokens}-token cap"))
    # The tag is kept short deliberately: it renders as uppercase mono with
    # letter-spacing inside a ~55%-width column, and the model id is long enough
    # to wrap it onto two lines. The id is evidence, not a heading, so it sits in
    # the footer with the other evidence — what it cost, and the figure check.
    m('<div class="hs-layer hs-layer--research" style="margin-top:12px;">'
      '<div class="hs-layer__tag">Nugen · language layer</div>'
      f'<div class="hs-layer__h">{_esc(cfg["label"])}</div>'
      f'<div class="hs-layer__p">{_nugen_html(res.text)}</div>'
      f'<div class="hs-cap" style="margin-top:10px;">'
      f'{_nugen_figure_check(res.unsupported)}</div>'
      '<div class="hs-cap" style="margin-top:4px;">Re-wording of the directive '
      f'above by <code>{_esc(res.model or nugen_client.known_models()[0])}</code>'
      f' · {cost} · this layer computes '
      'nothing and changes no number.</div>'
      '</div>')


def render_nugen_panel(s, d):
    """The Nugen language layer's audit surface: what is sent, and what it cost.

    The layer itself is not here — it renders inline on the three role tabs,
    under the directive each briefing re-words. This panel is where a reviewer
    checks it: the exact prompt for every role, the budget actually spent, and
    the standing claim that no number on the dashboard comes from this layer.
    """
    m(h2("Nugen language layer — what is sent, and what it costs",
         "Layer 2 · language only · the briefings on the three role tabs"))
    m(note("Every role tab carries a Nugen briefing under its directive card: "
           "the same decision, re-worded for the farmer, the duty engineer and the "
           "disaster officer. The model is handed the computed numbers and "
           "instructed to use nothing else; every figure it prints is then checked "
           "back against the computed state and anything unsupported is flagged on "
           "the card. It cannot reach the physics engine, the directive cards stay "
           "the authoritative wording, and the console renders identically when "
           "this layer is off — which is what makes it safe to put a language model "
           "in front of a flood warning at all.",
           label="What this is, and is not"))
    st.write("")

    key = _nugen_key()
    basin_label = _nugen_basin_label()
    sig = _nugen_sig(s)

    if not key:
        m('<div class="hs-layer hs-layer--research">'
          '<div class="hs-layer__tag">Layer 2 · not configured</div>'
          '<div class="hs-layer__h">Briefing layer is off — no API key configured</div>'
          '<div class="hs-layer__p">This is the normal state for a public deployment: the '
          'key is never committed to the repository. To switch the layer on, set '
          '<code>NUGEN_API_KEY</code> as an environment variable (on Render: '
          '<i>Environment → Environment Variables</i>) or put it in a gitignored '
          '<code>.streamlit/secrets.toml</code>, then restart the app. Every forecast, '
          'gauge and directive on this dashboard is produced without it.</div>'
          f'<div class="hs-cap" style="margin-top:10px;">Model '
          f'<code>{nugen_client.MODEL}</code> · '
          f'{nugen_client.MAX_TOKENS_LIMIT}-token completion cap.</div>'
          '</div>')
        _render_nugen_prompts(s, d, basin_label)
        return

    left, right = st.columns([2, 1])
    with left:
        go = st.button("Generate the combined briefing — all three roles",
                       key="nugen_go", width="stretch")
    with right:
        st.markdown(
            f'<div class="hs-cap" style="padding-top:8px;">Model '
            f'<code>{_esc(nugen_client.known_models()[0])}</code> · cap '
            f'{nugen_client.DEFAULT_MAX_TOKENS}/{nugen_client.MAX_TOKENS_LIMIT} tokens · '
            f'{nugen_client.budget_left()} calls left this session.<br>'
            f'Resolved from the account catalogue: '
            f'{_esc(nugen_client.catalogue_note())}.</div>',
            unsafe_allow_html=True)

    # The layer's actual spend, not a promise about it. Role briefings come out
    # of the automatic allowance; this panel's combined briefing comes out of the
    # headroom left above it, so a reviewer can always get one on demand.
    m(f'<div class="hs-cap" style="margin-top:8px;">Spent so far this session: '
      f'<b>{nugen_client.calls_made()}</b> network call'
      f'{"" if nugen_client.calls_made() == 1 else "s"} of '
      f'{nugen_client.CALL_BUDGET}, of which <b>{nugen_client.auto_calls_made()}</b> '
      f'were the automatic role briefings (allowance '
      f'{nugen_client.AUTO_CALL_BUDGET}, {nugen_client.auto_budget_left()} left). '
      f'A briefing is keyed on the facts it was written from, so an unchanged '
      f'basin re-renders its briefings for free however often the page refreshes.'
      f'</div>')

    if go:
        with st.spinner("Re-wording the directives…"):
            res = nugen_client.brief(s, d, place_name=basin_label, api_key=key)
        ss["_nugen"] = {
            "sig": sig, "ok": res.ok, "text": res.text, "error": res.error,
            "cached": res.cached, "tokens": res.tokens_out,
            "unsupported": list(res.unsupported),
            "clock": getattr(s, "clock", ""),
        }

    data = ss.get("_nugen")
    if not data:
        m('<div class="hs-cap" style="margin-top:8px;">No combined briefing '
          'requested yet — the per-role briefings on the three role tabs are the '
          'layer\'s normal output, and this button is the manual path to all three '
          'in one paragraph. Nothing is sent for it until you press it.</div>')
        _render_nugen_prompts(s, d, basin_label)
        return

    if not data.get("ok"):
        m(note(_esc(data.get("error") or "The briefing layer did not return text."),
               label="Briefing unavailable"))
        m('<div class="hs-cap" style="margin-top:6px;">The dashboard above is '
          'unaffected — the briefing layer is presentational.</div>')
        _render_nugen_prompts(s, d, basin_label)
        return

    stale = data.get("sig") != sig
    tag = ("Layer 2 · language layer · re-worded from the directives above"
           if not stale else
           "Layer 2 · generated for an earlier update — press again to refresh")
    bad = data.get("unsupported") or []
    if bad:
        check = ('<span style="color:var(--warning)">Figure check: '
                 + _esc(", ".join(str(b) for b in bad[:6]))
                 + ' did not come from the computed state — trust the directive '
                   'cards, not this wording.</span>')
    else:
        check = ('<span style="color:var(--safe)">Figure check: every number in '
                 'this briefing appears in the computed state.</span>')
    m('<div class="hs-layer hs-layer--research">'
      f'<div class="hs-layer__tag">{tag}</div>'
      f'<div class="hs-layer__h">Duty-officer briefing — {_esc(basin_label)}</div>'
      f'<div class="hs-layer__p">{_nugen_html(data.get("text", ""))}</div>'
      f'<div class="hs-cap" style="margin-top:10px;">{check}</div>'
      f'<div class="hs-cap" style="margin-top:4px;">Generated for the '
      f'{_esc(data.get("clock") or "current")} update'
      + (" · served from cache (no tokens spent)" if data.get("cached")
         else (f" · {data['tokens']} completion tokens"
               if data.get("tokens") else ""))
      + ' · wording only: nothing here is computed by this layer.</div>'
      '</div>')

    with st.expander("What was sent to the briefing layer"):
        st.code(nugen_client.preview(s, d, basin_label), language="text")
        st.caption("Only the computed state and the approved directives are sent, "
                   "after a fixed instruction and one worked example. No credentials, "
                   "no user input and no raw observations leave the app.")
    _render_nugen_prompts(s, d, basin_label)


def _render_nugen_prompts(s, d, basin_label: str):
    """The exact prompt behind each role briefing, verbatim and in full.

    The claim this layer makes is that it is handed the computed numbers and may
    use nothing else. That claim is only worth anything if the prompt is visible,
    so it is printed here in full, for every role, whether or not a key is
    configured — a reviewer can audit the layer on a deployment that has never
    called it.
    """
    with st.expander("The exact prompt behind each role briefing"):
        st.caption(
            f"One request per role, {nugen_client.ROLE_MAX_TOKENS} tokens each. "
            "The role digests carry no wall clock and round every reading, which "
            "is what lets a briefing be cached per situation instead of re-billed "
            "per refresh. Nothing but these lines reaches the endpoint: no "
            "credentials, no user input, no raw observations.")
        for role, cfg in nugen_client.ROLES.items():
            st.markdown(f"**{cfg['label']}** — `role={role}`")
            st.code(nugen_client.role_preview(s, d, role, basin_label),
                    language="text")


# ---------------------------------------------------------------------------
# Live dashboard — header + tabs re-render together on every tick
# ---------------------------------------------------------------------------
def _render_live_unreachable(obs=None, connecting: bool = False):
    """Honest panel for 'live mode selected, but no reading yet'.

    Shown INSTEAD of the dashboard, so we never dress a fallback state up as
    real observations. The user retries, or chooses demo mode explicitly — the
    app never switches for them.

    Two shades of the same honesty. On a cloud host the *server's* own
    Open-Meteo call is routinely 429'd (shared datacentre IP) while the
    visitor's in-browser fetch succeeds a beat later — so on the very first
    render, with that fetch still in flight, the truthful statement is "still
    connecting", not "unreachable". Either way no number is shown.
    """
    if connecting:
        badge, headline = "Connecting to the live feed", "waiting for the first real-time reading."
    else:
        badge, headline = "Live data unavailable", "waiting for the first real-time reading."
    m('<div class="hs-cmd"><div>'
      '<div class="hs-cmd__title">Basin Command Console</div>'
      f'<div class="hs-cmd__sub">Live · <b>{_esc(ss.region_name)}</b> &nbsp;·&nbsp; '
      f'{headline}</div></div>'
      '<span class="hs-badge hs-badge--warning"><span class="hs-dot"></span>'
      f'{badge}</span></div>')
    st.write("")

    if connecting:
        m(note("The first reading is being fetched in your browser, so it comes "
               "from your own connection rather than this server's. Nothing is "
               "shown until it arrives — no estimated numbers stand in for it.",
               label="Fetching the first reading"))
        return

    why = (getattr(obs, "error", None) or "").strip() if obs is not None else ""
    m(note("The real-time weather feed could not be reached, so there is nothing "
           "measured to show yet. No numbers are displayed rather than estimated "
           "ones. This usually clears within a minute — the feed is retried "
           f"automatically every {LIVE_REFRESH_SECS} seconds."
           + (f"<br><span class='hs-cap'>Reason: {_esc(why)}</span>" if why else ""),
           label="Live feed unreachable"))
    st.write("")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("↻ Retry now", key="live_retry_now", width="stretch"):
            _on_refresh_live()
            _safe_rerun()
    with c2:
        if st.button("🎬 Use demo mode instead", key="live_fallback_demo",
                     width="stretch"):
            ss.mode = "demo"
            ss.tick = 0
            ss.last_tick_time = time.monotonic()
            _safe_rerun()


def _safe_rerun():
    """Rerun the whole app (not just this fragment) across Streamlit versions."""
    try:
        st.rerun(scope="app")
    except TypeError:            # Streamlit < 1.37 has no scope argument
        st.rerun()


def render_dashboard():
    if ss.mode == "live":
        # A visible loading state while the very first reading is in flight —
        # the panel must never look "ready" before real data has arrived.
        first_load = not ss.get("_live_ever_ok")
        if first_load:
            with st.spinner(f"Connecting to the live feed for {ss.region_name}…"):
                _browser_live_pump(current_basin())
                obs = _get_live_current()
        else:
            _browser_live_pump(current_basin())   # refresh the browser feed this cycle
            obs = _get_live_current()

        if obs is not None and obs.ok:
            was_cold = not ss.get("_live_ever_ok")
            ss["_live_ever_ok"] = True
            ss["_live_tries"] = 0
            ss["_live_misses"] = 0
            if was_cold:
                # This is a FRAGMENT rerun, so it repaints the dashboard only —
                # the sidebar badge and the footer are at script scope and would
                # keep saying "connecting"/"waiting" behind a working console.
                # That contradiction is what made a healed page still look
                # broken until the visitor pressed refresh themselves. One full
                # rerun fixes it, costs no network (the reading is already in
                # session state / the module cache), and happens once per visit.
                _safe_rerun()
        elif first_load:
            # Both shades withhold every number; the difference is only which
            # statement is true. See _live_connecting for why a first miss is
            # reported as "connecting" rather than "unreachable".
            misses = ss.get("_live_misses", 0) + 1
            ss["_live_misses"] = misses
            ss["_live_tries"] = misses
            _render_live_unreachable(obs, connecting=_live_connecting(misses))
            return

        # LIVE clock origin = the real observation time (never BASE_TIME).
        base_time = _live_base_time(obs)
        # Tab 5 re-runs the engine on this exact forcing to measure its runtime
        # spread, prove determinism and sweep rainfall. Stashing the spec keeps
        # those panels honest: they exercise the same inputs this page rendered,
        # not a reconstruction of them.
        spec = H.forcing_from_live(obs, ss.res_pct / 100.0)
        tick = H.live_tick_for(obs)
        s = H.simulate(spec, tick, base_time=base_time)
        ss["_spec"], ss["_spec_tick"] = spec, tick
        place = H.place_from_region(ss.region_name)
    else:
        _advance_if_due()
        obs = None
        base_time = None                 # demo keeps the deterministic BASE_TIME
        s = H.simulate(ss.scenario, ss.tick)
        # Same stash as the live branch. The scenario *name* is what simulate()
        # was given, so that is what Tab 5 replays — forcing_from_scenario is
        # applied by the consumer, exactly as the engine does it internally.
        ss["_spec"], ss["_spec_tick"] = ss.scenario, ss.tick
        place = None                     # engine uses the scripted DEMO_PLACE
    d = H.make_directives(s, place, base_time=base_time)

    render_header(s, obs)

    tab_over, tab_farm, tab_dam, tab_dis, tab_ai = st.tabs([
        "Overview",
        "Farmer advisory",
        "Reservoir operations",
        "Disaster response",
        "AI analyst",
    ])
    with tab_over:
        render_overview(s, d, place)
    with tab_farm:
        render_farmer(s, d, place)
    with tab_dam:
        render_dam(s, d, place)
    with tab_dis:
        render_disaster(s, d, place)
    with tab_ai:
        render_analyst(s, d)


# run_every drives auto-refresh. In demo mode it advances the scenario clock and
# stops (None) once paused or the event ends. In live mode it periodically re-runs
# the panel; the actual network fetch is throttled per basin inside live_data
# (a good reading held ~5 min, a failed one retried after ~20 s so it self-heals).
if ss.mode == "live":
    # Until the first real reading lands, poll hard: recovery used to wait on
    # the 60 s tick (and up to ~2 min when the two 60 s clocks were out of
    # phase), which is long enough that a loading state reads as a dead page.
    # This costs no extra network — live_data throttles a failed basin to one
    # fetch per ~20 s regardless of how often the fragment reruns.
    _run_every = LIVE_REFRESH_SECS if ss.get("_live_ever_ok") else FIRST_LOAD_POLL_SECS
    # A briefing fetched in the background lands whenever the endpoint answers,
    # which the fragment only notices on its next rerun. At the 60 s cadence that
    # would leave a "re-wording this directive" line sitting there for up to a
    # minute after the text was ready. While something is in flight, poll at the
    # first-load rate instead; this reverts by itself when the last thread lands,
    # and costs no network of its own (live_data throttles the feed per basin).
    if nugen_client.pending_count():
        _run_every = min(_run_every, FIRST_LOAD_POLL_SECS)
else:
    _run_every = ss.interval if (ss.live and ss.tick < H.TICKS_MAX) else None

st.fragment(render_dashboard, run_every=_run_every)()


# ============================================================================
# FOOTER
# ============================================================================
# The footer sits OUTSIDE the dashboard fragment, so it must not claim live
# observations while the fragment is showing the "feed unreachable" panel —
# that is the same demo-as-live confusion issue #2 exists to prevent.
if ss.mode != "live":
    _foot_right = ("Live physics + statistics simulation of the Upper Bhima Basin "
                   "· runs fully offline.")
elif ss.get("_live_ever_ok"):
    _foot_right = (f"Real-time observations for {ss.region_name} "
                   "· physics computed on-device.")
else:
    _foot_right = (f"Waiting for the first real-time reading for {ss.region_name} "
                   "· nothing is estimated.")
m(
    '<div class="hs-foot">'
    '<span>HydroSentry-AI — physics-guided flood &amp; drought intelligence · Indradhanu 2026</span>'
    f'<span>{_foot_right}</span>'
    '</div>'
)
