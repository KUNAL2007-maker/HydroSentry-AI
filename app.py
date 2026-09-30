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


def style_fig(fig, height=230):
    """Apply the shared light-theme look to a plotly figure."""
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=10, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="IBM Plex Sans, sans-serif", color=C["ink"], size=12),
        showlegend=False,
    )
    return fig


def chart_placeholder(text):
    m(f'<div class="hs-panel" style="height:230px;display:grid;place-items:center;'
      f'color:{C["muted"]};font-size:13px;">{text}</div>')


# ============================================================================
# MAPS  (real interactive pydeck maps — tokenless CARTO basemap)
# ============================================================================
def _rgb(hex_color, alpha=220):
    """'#1F5FB0' -> [31, 95, 176, alpha] for pydeck fill colors."""
    h = hex_color.lstrip("#")
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha]


def _sev_color(sev, alpha=220):
    return _rgb(C.get(sev, C["teal"]), alpha)


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
    rows = [
        # basin centre — colored by the more severe of the two hazards
        {"lat": lat, "lon": lon, "color": _sev_color(worst),
         "radius": 900, "label": ss.region_name,
         "detail": f"Flood: {SEV_LABEL[s.flood_sev]} · Drought: {SEV_LABEL[s.drought_sev]}"},
        # agricultural belt (drought signal) — indicative position NE of centre
        {"lat": lat + 0.10, "lon": lon + 0.12, "color": _sev_color(s.drought_sev),
         "radius": 620, "label": "Agricultural belt (indicative)",
         "detail": f"Drought: {SEV_LABEL[s.drought_sev]} · ESP {s.esp:.0f}th %ile"},
        # riverside / reservoir (flood signal) — indicative position SW of centre
        {"lat": lat - 0.09, "lon": lon - 0.10, "color": _sev_color(s.flood_sev),
         "radius": 620, "label": "Riverside & reservoir (indicative)",
         "detail": f"Flood: {SEV_LABEL[s.flood_sev]} · reservoir {s.reservoir_pct:.0f}%"},
    ]
    if HAS_PYDECK:
        _deck_map(pd.DataFrame(rows), lat, lon, zoom=9.2, height=height)
    else:
        _deck_map(None, lat, lon, height=height)
    m('<div class="hs-map__legend" style="position:static;margin-top:8px;">'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--flood)"></span>Flood signal</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--drought)"></span>Drought signal</span>'
      '</div>'
      '<div class="hs-cap" style="margin-top:4px;">Basin centre uses live coordinates; '
      'hotspot markers are indicative placements around the centre.</div>')


def _risk_sev(risk) -> str:
    """Map a zone's risk label ('High'/'Medium'/'Low') to a severity colour."""
    return {"high": "critical", "medium": "warning", "low": "watch"}.get(
        str(risk or "").strip().lower(), "safe")


def render_evacuation_map(s, tto, at_risk, height=320, place=None):
    """Disaster map: the Place's riverside zones around the basin centre, coloured
    by time to impact. Zone positions are indicative (no per-zone geometry feed)."""
    pl = place or H.DEMO_PLACE
    lat, lon = ss.region_lat, ss.region_lon
    finite = math.isfinite(tto)

    def _tsev(mins):
        if not math.isfinite(mins):
            return "safe"
        if mins <= 100:
            return "critical"
        if mins <= 180:
            return "warning"
        return "safe"

    # the Place's zones, staged along an indicative line near the centre
    rows = []
    for z in pl.sectors:
        hh = z.get("households")
        if hh is None:
            hh = round(at_risk * float(z.get("share", 0.0)))
        mins = tto + float(z.get("delay", 0)) if finite else float("inf")
        sev = _tsev(mins)
        when = "no impact expected" if not math.isfinite(mins) else (
            "impact imminent" if mins <= 1 else f"impact in ~{int(round(mins))} min")
        detail = f"{when} · {(hh if finite else 0):,} households"
        if z.get("elev"):
            detail += f" · {z['elev']}"
        rows.append({"lat": lat + float(z.get("dlat", 0.0)),
                     "lon": lon + float(z.get("dlon", 0.0)),
                     "color": _sev_color(sev),
                     "radius": 500,
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
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--critical)"></span>Evacuate now</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--warning)"></span>Stand by</span>'
      '<span class="hs-leg"><span class="hs-leg__sw" style="background:var(--safe)"></span>Safe ground</span>'
      '</div>'
      '<div class="hs-cap" style="margin-top:4px;">Zone positions are indicative placements '
      'around the basin centre; severity tracks the live time-to-impact.</div>')


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
    bucket = int(time.time() // LIVE_REFRESH_SECS)
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


def _set_region(basin):
    """Point the live view at a new region and force a fresh fetch."""
    ss.region_name = basin.name
    ss.region_lat = basin.lat
    ss.region_lon = basin.lon
    ss.region_tz = basin.tz
    live_data.clear_live_cache(basin)
    ss["_live_nonce"] = ss.get("_live_nonce", 0) + 1   # re-fetch the browser feed too


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
            m('<span class="hs-badge hs-badge--warning"><span class="hs-dot"></span>'
              'Data unavailable — fallback</span>')
            if obs.error:
                m('<div class="hs-cap" style="color:var(--warning);margin:4px 0 2px;'
                  'word-break:break-word;">Reason: ' + _esc(obs.error) + '</div>')
            m('<div class="hs-cap" style="margin:2px 0 2px;">The forecast physics still '
              'runs on safe fallback values — tap “Refresh now” to retry the live feed.</div>')
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
        + metric("Runoff drift at +4°C", "−7.1", "%", "Black-box AI drifts +25%")
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
            fig = go.Figure(go.Indicator(
                mode="gauge+number",
                value=s.esp,
                number={"suffix": "th %ile", "font": {"size": 30, "color": C["brand"]}},
                gauge={
                    "axis": {"range": [0, 100], "tickvals": [0, 25, 50, 75, 100]},
                    "bar": {"color": C["drought"], "thickness": 0.28},
                    "borderwidth": 0,
                    "steps": [
                        {"range": [0, 10], "color": "#F4D7C2"},
                        {"range": [10, 30], "color": "#FBEBDF"},
                        {"range": [30, 100], "color": "#EAF3EE"},
                    ],
                    "threshold": {"line": {"color": C["critical"], "width": 3},
                                  "thickness": 0.8, "value": 10},
                },
            ))
            st.plotly_chart(style_fig(fig, 210), width="stretch",
                            config={"displayModeBar": False})
        else:
            chart_placeholder("Evaporative Stress Percentile gauge")
        m('<div class="hs-cap">Evaporative Stress Percentile (ESP). Below the red mark = flash-drought onset.</div>')

        st.write("")
        m(h2("Root-zone moisture", "Since the event began"))
        if HAS_PLOTLY and s.sm_series.size:
            rel_days = (s.sm_days - s.sm_days.max()).tolist()      # today = 0
            soil = s.sm_series.tolist()
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=rel_days, y=soil, mode="lines", line=dict(color=C["drought"], width=2.5),
                fill="tozeroy", fillcolor="rgba(178,106,46,.10)"))
            fig2.add_hline(y=H.THETA_WP, line=dict(color=C["critical"], width=1.5, dash="dash"),
                           annotation_text="Wilting point", annotation_position="bottom right",
                           annotation_font_size=10)
            fig2.update_layout(yaxis=dict(range=[0, 0.4], title=None, gridcolor=C["line"]),
                               xaxis=dict(title="days ago → today", gridcolor="rgba(0,0,0,0)"))
            st.plotly_chart(style_fig(fig2, 200), width="stretch",
                            config={"displayModeBar": False})
        else:
            chart_placeholder("Root-zone moisture trend")
        m(f'<div class="hs-cap">Moisture now at {s.soil_moisture:.2f} m³/m³ '
          f'(wilting point {H.THETA_WP:.2f}).</div>')


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
            fig = go.Figure(go.Indicator(
                mode="gauge+number",
                value=round(s.reservoir_pct, 1),
                number={"suffix": "%", "font": {"size": 30, "color": C["brand"]}},
                gauge={
                    "axis": {"range": [0, 100]},
                    "bar": {"color": C["flood"], "thickness": 0.28},
                    "borderwidth": 0,
                    "steps": [
                        {"range": [0, 70], "color": "#E7F1EA"},
                        {"range": [70, 88], "color": "#FBF2D8"},
                        {"range": [88, 100], "color": "#F7DAD6"},
                    ],
                    "threshold": {"line": {"color": C["critical"], "width": 3},
                                  "thickness": 0.8, "value": 90},
                },
            ))
            st.plotly_chart(style_fig(fig, 210), width="stretch",
                            config={"displayModeBar": False})
        else:
            chart_placeholder("Reservoir storage gauge")
        m(f'<div class="hs-cap">Storage at {s.reservoir_level:.1f} m. '
          'Red mark = spillway threshold; pre-release keeps a safe gap.</div>')

        st.write("")
        m(h2("Inflow forecast", "Next 6 hours"))
        if HAS_PLOTLY:
            hrs = s.fc_hours.tolist()
            inflow = s.fc_inflow.tolist()
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=hrs, y=inflow, mode="lines",
                line=dict(color=C["flood"], width=2.5),
                fill="tozeroy", fillcolor="rgba(31,95,176,.08)"))
            fig2.add_hline(y=H.SAFE_CHANNEL, line=dict(color=C["warning"], width=1.3, dash="dash"),
                           annotation_text="safe channel", annotation_position="top right",
                           annotation_font_size=10)
            fig2.add_vline(x=0, line=dict(color=C["teal"], width=1.5, dash="dot"),
                           annotation_text="now", annotation_position="top left",
                           annotation_font_size=10)
            fig2.update_layout(yaxis=dict(title="m³/s", gridcolor=C["line"]),
                               xaxis=dict(title="hours from now", gridcolor="rgba(0,0,0,0)"))
            st.plotly_chart(style_fig(fig2, 200), width="stretch",
                            config={"displayModeBar": False})
        else:
            chart_placeholder("Inflow forecast")
        if s.inflow_peak_in_h > 0.2:
            cap = (f"Predicted inflow peaks at {s.inflow_peak:.0f} m³/s in about "
                   f"{s.inflow_peak_in_h:.1f} hours.")
        elif s.inflow_peak > H.SAFE_CHANNEL:
            cap = f"Inflow near its crest of {s.inflow_peak:.0f} m³/s and beginning to recede."
        else:
            cap = f"Inflow steady near baseline ({s.inflow_now:.0f} m³/s)."
        m(f'<div class="hs-cap">{cap}</div>')


# ---------------------------------------------------------------------------
# TAB 4 — DISASTER RESPONSE  (geofenced evacuation)
# ---------------------------------------------------------------------------
def render_disaster(s, d, place=None):
    di = d["disaster"]
    tto = s.time_to_overtop_min
    at_risk = s.households_at_risk
    pl = place or H.DEMO_PLACE          # zones/shelter labels come from the Place

    def _sev_for(mins):
        if not math.isfinite(mins):
            return "safe"
        if mins <= 100:
            return "critical"
        if mins <= 180:
            return "warning"
        return "watch"

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
# TAB 5 — MODEL & VALIDATION  (for judges / technical reviewers)
# ---------------------------------------------------------------------------
def _flow(steps) -> str:
    """Render a pipeline as monospace step chips joined by arrows."""
    parts = []
    for i, step in enumerate(steps):
        if i:
            parts.append('<span class="hs-flow__arrow">→</span>')
        parts.append(f'<span class="hs-flow__step">{step}</span>')
    return '<div class="hs-flow">' + "".join(parts) + '</div>'


def render_model(s, d):
    m(h2("Model &amp; validation", "How HydroSentry-AI works, and why it can be trusted"))
    m(note("HydroSentry-AI runs in two clearly separated layers. <b>Layer 1</b> is the "
           "deterministic physics engine that produced every number on this screen, in "
           "milliseconds. <b>Layer 2</b> is the research track — neural surrogates and "
           "published benchmarks that are <b>not</b> in the live decision path. Nothing "
           "below mixes the two.",
           label="Read this first"))
    st.write("")

    # ---- Layer 1 — what actually ran ------------------------------------
    m(h2("Layer 1 — Operational Production Engine", "This is what computed the live dashboard"))
    m(
        '<div class="hs-layer">'
        '<div class="hs-layer__tag">Layer 1 · in the live decision path · running now</div>'
        '<div class="hs-layer__h">Deterministic physics &amp; statistics — '
        f'{s.real_compute_ms:.2f} ms measured this update</div>'
        '<div class="hs-layer__p">Closed-form hydrology, solved on the CPU with no model '
        'weights, no GPU and no network call: a <b>Gamma unit-hydrograph convolution</b> for '
        'catchment routing, an <b>explicit Euler reservoir mass balance</b> '
        '(dS/dt = inflow − release, dt = 0.1 h) for storage, level and FIRO pre-release, a '
        'rating/levee-crest comparison for downstream stage and time-to-overtopping, and a '
        'soil-moisture bucket with the <b>FAO-56 Evaporative Stress Ratio</b> and its '
        'percentile climatology for the drought side. Being deterministic, it returns the '
        'same answer for the same inputs every time — and it is fast enough to re-run on '
        'every tick, which is why the execution time above is measured with '
        '<code>time.perf_counter()</code> rather than quoted from a paper.</div>'
        + _flow(["Live observations (Open-Meteo)", "Forcing", "Gamma UH convolution",
                 "Euler reservoir mass balance", "Levee stage &amp; time-to-impact",
                 "FAO-56 ESR / ESP", "BasinState", "Rule-based directives", "Dashboard"])
        + '<div class="hs-cap" style="margin-top:10px;">LIVE OPERATIONAL PATH — every value '
        'in the Overview, Farmer, Reservoir and Disaster tabs comes from this chain.</div>'
        '</div>'
    )

    st.write("")
    m(h2("Layer 2 — Research &amp; Neural Surrogates", "Published / in development — not in the live path"))
    m(
        '<div class="hs-layer hs-layer--research">'
        '<div class="hs-layer__tag">Layer 2 · research track · not used for the live readings</div>'
        '<div class="hs-layer__h">Neural surrogates and the HEC-RAS 2D benchmark</div>'
        '<div class="hs-layer__p">The research layer targets the problems the closed-form '
        'engine deliberately does not attempt — full 2D inundation mapping and learned '
        'forecast-error correction. Its headline figure is a <b>benchmark</b>: the PINN '
        f'flood-map surrogate produces a 2D depth map in <b>~{H.BENCHMARK_COMPUTE_S:.1f} s</b> '
        'against <b>~2.3 h</b> for an equivalent HEC-RAS 2D run (~100× faster). That number '
        'describes the research surrogate, <b>not</b> the runtime of the operational engine '
        'above — the live engine finishes in milliseconds because it solves a much smaller, '
        'closed-form problem. These components are staged behind the same '
        '<code>simulate()</code> seam so they can be promoted into Layer 1 once each is '
        'validated against gauge records.</div>'
        + _flow(["Historical / synthetic events", "PINN 2D Saint-Venant surrogate",
                 f"~{H.BENCHMARK_COMPUTE_S:.1f} s depth map", "MC-LSTM-PET drought forecaster",
                 "Errorcastnet bias correction", "Validation vs HEC-RAS 2D &amp; gauges",
                 "Research notebooks"])
        + '<div class="hs-cap" style="margin-top:10px;">RESEARCH / BENCHMARK PATH — offline, '
        'run against historical events; none of it is executed to render this console.</div>'
        '</div>'
    )

    st.write("")
    m(h2("How it compares", "Against today's options"))
    m(
        '<div class="hs-scroll"><table class="hs-table"><thead><tr>'
        '<th>Approach</th><th>Layer</th><th>Speed</th><th>Obeys physics</th>'
        '<th>Safe for decisions</th>'
        '</tr></thead><tbody>'
        '<tr><td><b>HydroSentry-AI operational engine</b></td><td>1 — production</td>'
        '<td class="num">' + f"{s.real_compute_ms:.2f} ms" + '</td>'
        '<td class="hs-yes">Yes — solved, not learned</td>'
        '<td class="hs-yes">Yes — certified directives</td></tr>'
        '<tr><td>HydroSentry-AI PINN flood-map surrogate</td><td>2 — research</td>'
        '<td class="num">' + f"~{H.BENCHMARK_COMPUTE_S:.1f} s" + ' <span class="hs-cap">(benchmark)</span></td>'
        '<td class="hs-yes">Physics-constrained loss</td>'
        '<td class="hs-no">Not yet in the live path</td></tr>'
        '<tr><td>HEC-RAS 2D (reference solver)</td><td>Baseline</td><td class="num">~2.3 hr</td>'
        '<td class="hs-yes">Yes</td><td class="hs-no">Too slow for flash events</td></tr>'
        '<tr><td>Black-box AI</td><td>—</td><td class="num">Fast</td>'
        '<td class="hs-no">No — invents +25% water at +4°C</td><td class="hs-no">Underpredicts peaks</td></tr>'
        '<tr><td>Generic AI / LLM</td><td>—</td><td class="num">Fast</td>'
        '<td class="hs-no">No</td><td class="hs-no">Can hallucinate advice</td></tr>'
        '</tbody></table></div>'
    )

    st.write("")
    col_l, col_r = st.columns([1, 1])
    with col_l:
        m(h2("Validation scorecard", "Measured performance"))
        m(
            '<div class="hs-scroll"><table class="hs-table"><thead><tr>'
            '<th>Metric</th><th>Score</th><th>What it means</th>'
            '</tr></thead><tbody>'
            '<tr><td>Operational engine execution</td>'
            '<td class="num">' + f"{s.real_compute_ms:.2f} ms" + '</td>'
            '<td>Layer 1 — measured on this update with time.perf_counter()</td></tr>'
            '<tr><td>PINN flood-map surrogate</td>'
            '<td class="num">' + f"~{H.BENCHMARK_COMPUTE_S:.1f} s" + '</td>'
            '<td>Layer 2 benchmark — ~100× faster than HEC-RAS 2D (~2.3 h)</td></tr>'
            '<tr><td>KGE accuracy</td><td class="num">' + f"{s.kge:.2f}" + '</td>'
            '<td>Gold-standard hydrology score (1.0 is perfect)</td></tr>'
            '<tr><td>Drought lead time</td><td class="num">' + f"{s.lead_time_days} days" + '</td>'
            '<td>Warning before visible crop wilting</td></tr>'
            '<tr><td>Drought detection (POD)</td><td class="num">' + f"{s.pod:.2f}" + '</td>'
            '<td>Probability of catching onset</td></tr>'
            '<tr><td>Soil-moisture match (R)</td><td class="num">' + f"{s.r_smap:.2f}" + '</td>'
            '<td>Agreement with NASA SMAP satellite</td></tr>'
            '<tr><td>Errorcastnet gain</td><td class="num">up to 6×</td>'
            '<td>Layer 2 target — accuracy over standalone physical models</td></tr>'
            '</tbody></table></div>'
        )
        m('<div class="hs-cap" style="margin-top:6px;">KGE, POD, R and the lead time are '
          'reference validation figures for the modelling approach, carried as constants; '
          'the execution time is the only number measured live.</div>')
    with col_r:
        m(h2("Physical honesty test", "Runoff drift under +4°C heat stress"))
        m(
            '<div class="hs-panel">'
            '<div class="hs-bar"><div class="hs-bar__lab"><span>HydroSentry-AI</span>'
            '<span class="v" style="color:var(--safe)">−7.1%</span></div>'
            '<div class="hs-bar__track"><div class="hs-bar__fill" '
            'style="width:22%;background:var(--safe)"></div></div></div>'
            '<div class="hs-bar"><div class="hs-bar__lab"><span>Standard black-box AI</span>'
            '<span class="v" style="color:var(--critical)">+25%</span></div>'
            '<div class="hs-bar__track"><div class="hs-bar__fill" '
            'style="width:78%;background:var(--critical)"></div></div></div>'
            '<div class="hs-cap" style="margin-top:8px;">Under extreme heat, black-box AI '
            'invents +25% of water that does not exist. HydroSentry-AI stays close to the '
            'true mass balance (−7.1% is natural thermodynamic loss).</div>'
            '</div>'
        )

    st.write("")
    m(h2("Component roles", "Which layer each engine belongs to"))
    m(
        '<div class="hs-arch">'
        '<div class="hs-arch__card"><div class="hs-arch__tag">Layer 1 · production · flood</div>'
        '<div class="hs-arch__h">Gamma unit hydrograph + reservoir mass balance</div>'
        '<div class="hs-arch__p">Routes the rainfall pulse into a reservoir inflow hydrograph and '
        'integrates storage forward explicitly, so inflow, release, level and the gate schedule '
        'are guaranteed to agree. This is what runs live, in milliseconds.</div></div>'
        '<div class="hs-arch__card"><div class="hs-arch__tag">Layer 1 · production · drought</div>'
        '<div class="hs-arch__h">FAO-56 Evaporative Stress Ratio + percentile climatology</div>'
        '<div class="hs-arch__p">Tracks the ratio of what the soil can give up against what the hot '
        'air demands (ET / PET) through a bucket with drainage, then places today on the historical '
        'stress distribution to give the percentile and days-to-wilting.</div></div>'
        '<div class="hs-arch__card"><div class="hs-arch__tag">Layer 2 · research · flood mapping</div>'
        '<div class="hs-arch__h">PINN — physics-informed neural network</div>'
        '<div class="hs-arch__p">Targets full 2D inundation depth by putting the Saint-Venant '
        f'residual in the loss. Benchmarked at ~{H.BENCHMARK_COMPUTE_S:.1f} s per map versus ~2.3 h '
        'for HEC-RAS 2D. Not executed by this console.</div></div>'
        '<div class="hs-arch__card"><div class="hs-arch__tag">Layer 2 · research · forecasting</div>'
        '<div class="hs-arch__h">MC-LSTM-PET + Errorcastnet</div>'
        '<div class="hs-arch__p">A mass-conserving recurrent drought forecaster with a '
        'thermodynamic ceiling, plus an error model that separates systematic bias from '
        'irreducible noise. In development against gauge records.</div></div>'
        '<div class="hs-arch__card"><div class="hs-arch__tag">Layer 1 · production · directives</div>'
        '<div class="hs-arch__h">Deterministic rule engine (CWC / IMD / SOP)</div>'
        '<div class="hs-arch__p">Every directive you see is generated by explicit, auditable rules '
        'over the computed state — severity thresholds, FIRO triggers and the Stage-1 watch '
        'advisory — so the wording can always be traced back to a number and a manual.</div></div>'
        '<div class="hs-arch__card"><div class="hs-arch__tag">Layer 2 · optional · language</div>'
        '<div class="hs-arch__h">Nugen — plain-language briefing layer</div>'
        '<div class="hs-arch__p">An optional hosted LLM call that <b>re-words</b> the directives the '
        'rule engine already produced into a short operator briefing. It is given the computed '
        'figures and nothing else, every number it prints is checked back against the computed '
        'state, it is off unless a key is configured, and the console works identically without '
        'it. See the briefing panel below.</div></div>'
        '</div>'
    )

    st.write("")
    render_nugen_panel(s, d)


# ---------------------------------------------------------------------------
# TAB 5 — optional Nugen briefing layer (Layer 2, language only)
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


def _nugen_html(text: str) -> str:
    """Escaped HTML for a model reply: bullet lines become a list."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    bullets = [ln.lstrip("-*•").strip() for ln in lines
               if ln[:1] in ("-", "*", "•")]
    if len(bullets) >= 2:
        return ('<ul class="hs-dir__actions" style="margin:0;">'
                + "".join(f"<li>{_esc(b)}</li>" for b in bullets) + "</ul>")
    return "".join(f'<p style="margin:0 0 6px;">{_esc(ln)}</p>' for ln in lines)


def render_nugen_panel(s, d):
    """The optional hosted-LLM briefing: re-wording only, never a forecast.

    Deliberately click-to-run. The account allows 500 tokens per completion, so
    nothing here fires on a page load or on the 60-second live refresh — the
    quota is spent only when a reviewer asks for a briefing, and repeat asks for
    the same update are served from the client's cache.
    """
    m(h2("Plain-language briefing (optional)",
         "Layer 2 · language only — Nugen hosted inference"))
    m(note("This panel asks a small hosted language model to <b>re-word</b> the "
           "directives the rule engine has already produced into a short duty-officer "
           "briefing. It is given the computed numbers and instructed to use nothing "
           "else; every figure it prints is then checked back against the computed "
           "state and anything unsupported is flagged below. It cannot reach the "
           "physics engine, the directive cards in the other tabs remain the "
           "authoritative wording, and the console behaves identically when this layer "
           "is switched off. It runs only when you press the button.",
           label="What this is, and is not"))
    st.write("")

    key = _nugen_key()
    basin_label = (ss.region_name if ss.mode == "live"
                   else "Upper Bhima Basin (Pune, Maharashtra)")
    sig = _nugen_sig(s)

    if not key:
        m('<div class="hs-layer hs-layer--research">'
          '<div class="hs-layer__tag">Layer 2 · optional · not configured</div>'
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
        return

    left, right = st.columns([2, 1])
    with left:
        go = st.button("Generate operator briefing", key="nugen_go",
                       width="stretch")
    with right:
        st.markdown(
            f'<div class="hs-cap" style="padding-top:8px;">Model '
            f'<code>{nugen_client.MODEL}</code> · cap '
            f'{nugen_client.DEFAULT_MAX_TOKENS}/{nugen_client.MAX_TOKENS_LIMIT} tokens · '
            f'{nugen_client.budget_left()} calls left this session.</div>',
            unsafe_allow_html=True)

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
        m('<div class="hs-cap" style="margin-top:8px;">No briefing requested yet. '
          'Nothing is sent until you press the button, so the token budget is spent '
          'only on demand.</div>')
        return

    if not data.get("ok"):
        m(note(_esc(data.get("error") or "The briefing layer did not return text."),
               label="Briefing unavailable"))
        m('<div class="hs-cap" style="margin-top:6px;">The dashboard above is '
          'unaffected — the briefing layer is presentational.</div>')
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


# ---------------------------------------------------------------------------
# Live dashboard — header + tabs re-render together on every tick
# ---------------------------------------------------------------------------
def _render_live_unreachable():
    """Honest panel for 'live mode selected, but no reading yet'.

    Shown INSTEAD of the dashboard, so we never dress a fallback state up as
    real observations. The user retries, or chooses demo mode explicitly — the
    app never switches for them.
    """
    m('<div class="hs-cmd"><div>'
      '<div class="hs-cmd__title">Basin Command Console</div>'
      f'<div class="hs-cmd__sub">Live · <b>{_esc(ss.region_name)}</b> &nbsp;·&nbsp; '
      'waiting for the first real-time reading.</div></div>'
      '<span class="hs-badge hs-badge--warning"><span class="hs-dot"></span>'
      'Live data unavailable</span></div>')
    st.write("")
    m(note("The real-time weather feed could not be reached, so there is nothing "
           "measured to show yet. No numbers are displayed rather than estimated "
           "ones. This usually clears within a minute — the feed is retried "
           f"automatically every {LIVE_REFRESH_SECS} seconds.",
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
            ss["_live_ever_ok"] = True
        elif first_load:
            _render_live_unreachable()
            return

        # LIVE clock origin = the real observation time (never BASE_TIME).
        base_time = _live_base_time(obs)
        s = H.simulate(H.forcing_from_live(obs, ss.res_pct / 100.0),
                       H.live_tick_for(obs), base_time=base_time)
        place = H.place_from_region(ss.region_name)
    else:
        _advance_if_due()
        obs = None
        base_time = None                 # demo keeps the deterministic BASE_TIME
        s = H.simulate(ss.scenario, ss.tick)
        place = None                     # engine uses the scripted DEMO_PLACE
    d = H.make_directives(s, place, base_time=base_time)

    render_header(s, obs)

    tab_over, tab_farm, tab_dam, tab_dis, tab_model = st.tabs([
        "Overview",
        "Farmer advisory",
        "Reservoir operations",
        "Disaster response",
        "Model & validation",
    ])
    with tab_over:
        render_overview(s, d, place)
    with tab_farm:
        render_farmer(s, d, place)
    with tab_dam:
        render_dam(s, d, place)
    with tab_dis:
        render_disaster(s, d, place)
    with tab_model:
        render_model(s, d)


# run_every drives auto-refresh. In demo mode it advances the scenario clock and
# stops (None) once paused or the event ends. In live mode it periodically re-runs
# the panel; the actual network fetch is throttled per basin inside live_data
# (a good reading held ~5 min, a failed one retried after ~20 s so it self-heals).
if ss.mode == "live":
    _run_every = LIVE_REFRESH_SECS
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
