"""
hydro_engine.py — HydroSentry-AI simulation core
=================================================

A self-contained, offline PHYSICS + STATISTICS engine for the Upper Bhima
Basin. It produces a live-evolving basin state from genuine calculations —
no random noise, no hard-coded dashboard numbers, no external data.

What is actually computed
-------------------------
Flood / reservoir
  * Catchment rainfall pulse  -> runoff volume
  * Gamma unit hydrograph     -> reservoir inflow hydrograph (lag + attenuation)
  * Reservoir mass balance    -> storage, level, spill   (dS/dt = inflow - release)
  * FIRO buffer logic         -> forecast-informed pre-release recommendation
  * Rating / levee crest      -> downstream stage, time-to-overtopping, depth

Drought / agriculture
  * Temperature-driven PET    -> atmospheric demand (Hargreaves-style)
  * Soil-moisture bucket      -> theta(t) with drainage + actual ET
  * Evaporative Stress Ratio  -> ESR = ET_actual / PET
  * Percentile climatology    -> ESP (where today's stress sits vs history)
  * Forward projection        -> days-to-wilting

Everything is a pure function of (scenario, tick), so the UI can render any
moment, step forward, or replay — and real models later replace `simulate()`
without touching the dashboard.

This is a SIMULATION for demonstration. Reservoir capacity and a few basin
constants are illustrative, chosen so the numbers stay internally consistent
and close to the project's narrative figures.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np


# ============================================================================
# Scenarios
# ============================================================================
SCENARIOS = {
    "normal": {
        "label": "Normal operations",
        "desc": "Calm baseline — light rain, healthy soil.",
        "rain_peak": 0.6,     # catchment-avg effective rainfall peak (mm/hr)
        "heat": 0.0,          # heatwave temperature offset (°C)
        "dry": False,
    },
    "flash_flood": {
        "label": "Flash flood",
        "desc": "Cloudburst over the Western Ghats drives a fast inflow surge.",
        "rain_peak": 7.2,
        "heat": 0.0,
        "dry": False,
    },
    "flash_drought": {
        "label": "Flash drought",
        "desc": "Heatwave + dry spell desiccates the eastern agri belt.",
        "rain_peak": 0.1,
        "heat": 8.0,
        "dry": True,
    },
    "dipole": {
        "label": "Dipole crisis",
        "desc": "Cloudburst in the west and flash drought in the east at once.",
        "rain_peak": 7.2,
        "heat": 8.0,
        "dry": True,
    },
}
SCENARIO_ORDER = ["normal", "flash_flood", "flash_drought", "dipole"]

TICKS_MAX = 32                 # a full scenario runs over 32 update ticks
FLOOD_HORIZON_H = 8.0          # flood dynamics span 8 hours
DROUGHT_HORIZON_D = 14.0       # drought dynamics span 14 days

# Sim clock origin for DEMO mode. The canned scenarios are a scripted historical
# story, so their clock is fixed and deterministic. LIVE mode must NOT use this —
# it passes the real observation time in as ``base_time`` (see _clock_base).
BASE_TIME = datetime(2026, 9, 21, 12, 0)   # demo sim clock origin (IST)

# Published RESEARCH benchmark for the PINN flood-map surrogate (vs ~2.3 h for a
# HEC-RAS 2D run). This is a *benchmark* figure from the research layer, NOT the
# runtime of the operational engine below — that is measured per call into
# ``BasinState.real_compute_ms`` with time.perf_counter().
BENCHMARK_COMPUTE_S = 82.9


# ============================================================================
# Basin constants (illustrative but internally consistent)
# ============================================================================
# Flood / reservoir
BASEFLOW = 190.0               # m³/s baseline inflow
UH_K, UH_THETA = 3.2, 2.05     # gamma unit-hydrograph shape/scale -> peak ~4.5 h
RES_CAP_AFT = 150_000.0        # reservoir capacity (acre-ft)
RES_START_FRAC = 0.78          # storage at scenario start
BASE_RELEASE = 120.0           # m³/s normal downstream release
SAFE_CHANNEL = 600.0           # m³/s safe downstream channel capacity
LEVEE_Q = 700.0                # m³/s at which the levee begins to overtop
MS_TO_AFT_PER_H = 3600.0 / 1233.48   # m³/s -> acre-ft per hour  (~2.918)
LEVEL_MIN, LEVEL_MAX = 55.0, 63.5    # reservoir level (m) at empty/full

# Drought / soil
THETA_FC, THETA_WP = 0.34, 0.10      # field capacity / wilting point (m³/m³)
ROOT_DEPTH_MM = 600.0                # root-zone depth
ESR_CLIM_MU, ESR_CLIM_SD = 0.70, 0.20   # historical ESR climatology (for ESP)
HH_TOTAL = 1030                      # households in the low-lying sectors


# ============================================================================
# State container
# ============================================================================
@dataclass
class BasinState:
    scenario: str
    tick: int
    flood_hours: float
    drought_day: float
    clock: str

    # clock origin this state was computed against: BASE_TIME in demo mode, the
    # real observation timestamp in live mode. gate_schedule() / make_directives()
    # fall back to it so every generated timestamp shares one base.
    base_time: datetime | None = None

    # flood / reservoir
    rain_now: float = 0.0
    inflow_now: float = 0.0
    inflow_peak: float = 0.0
    inflow_peak_in_h: float = 0.0
    past_peak: bool = False
    fc_hours: np.ndarray = field(default_factory=lambda: np.array([]))
    fc_inflow: np.ndarray = field(default_factory=lambda: np.array([]))
    reservoir_pct: float = 0.0
    reservoir_level: float = 0.0
    storage_aft: float = 0.0
    release_now: float = 0.0
    firo_release: float = 0.0
    target_buffer_aft: float = 0.0
    buffer_now_aft: float = 0.0
    spilling: bool = False
    start_frac: float = RES_START_FRAC   # storage fraction at event start
    target_peak: float = 0.0             # implied reservoir inflow peak (m³/s)
    flood_mode: bool = False             # surge above safe channel?

    # disaster
    q_downstream: float = 0.0
    overtop_depth: float = 0.0
    time_to_overtop_min: float = float("inf")
    households_at_risk: int = 0

    # drought / agri
    temp: float = 0.0
    pet: float = 0.0
    et_actual: float = 0.0
    esr: float = 0.0
    esp: float = 0.0
    soil_moisture: float = 0.0
    sm_days: np.ndarray = field(default_factory=lambda: np.array([]))
    sm_series: np.ndarray = field(default_factory=lambda: np.array([]))
    days_to_wilting: float = float("inf")

    # severities
    flood_sev: str = "safe"
    drought_sev: str = "safe"

    # ---- timing -----------------------------------------------------------
    # real_compute_ms: MEASURED wall-clock time of the operational hydrology
    # computation in this very simulate() call (time.perf_counter). This is the
    # number the dashboard shows as the production engine's execution time.
    real_compute_ms: float = 0.0
    # compute_time_s: the RESEARCH benchmark for the PINN flood-map surrogate
    # (~82.9 s vs ~2.3 h for HEC-RAS 2D). Kept for the benchmark comparison
    # table only — it is NOT this engine's runtime. See BENCHMARK_COMPUTE_S.
    compute_time_s: float = BENCHMARK_COMPUTE_S

    # model performance (validation constants)
    kge: float = 0.93
    pod: float = 0.57
    r_smap: float = 0.89
    lead_time_days: int = 10


# ============================================================================
# Helpers
# ============================================================================
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clock_base(base_time: "datetime | None" = None,
                s: "BasinState | None" = None) -> datetime:
    """The clock origin every generated timestamp must be derived from.

    Resolution order:
      1. an explicit ``base_time`` argument  (LIVE mode: the observation time)
      2. ``s.base_time`` stamped by ``simulate()``  (so a caller that forgets to
         pass base_time still cannot silently fall back to the historical clock)
      3. ``BASE_TIME``  (DEMO mode: the fixed, deterministic scripted clock)

    The returned value may be timezone-aware (live) or naive (demo); it is only
    ever added to a timedelta and formatted, never compared against the other
    kind, so the two modes cannot raise an offset-naive/aware TypeError.
    """
    if base_time is not None:
        return base_time
    if s is not None and getattr(s, "base_time", None) is not None:
        return s.base_time
    return BASE_TIME


def _trapz(y: np.ndarray, x: np.ndarray) -> float:
    """Trapezoidal integral, version-safe across numpy 1.x / 2.x."""
    fn = getattr(np, "trapezoid", None) or getattr(np, "trapz")
    return float(fn(y, x))


def _uh(t: float) -> float:
    """Gamma unit hydrograph ordinate at time t (hours)."""
    if t <= 0:
        return 0.0
    return (t ** (UH_K - 1)) * math.exp(-t / UH_THETA) / (UH_THETA ** UH_K * math.gamma(UH_K))


_UH_PEAK_T = (UH_K - 1) * UH_THETA          # ~4.5 h
_UH_PEAK_V = _uh(_UH_PEAK_T)

# faster unit hydrograph for LOCAL runoff below the dam (urban flash-flood driver)
UH2_K, UH2_THETA = 2.6, 1.4                 # peak ~2.2 h


def _uh2(t: float) -> float:
    if t <= 0:
        return 0.0
    return (t ** (UH2_K - 1)) * math.exp(-t / UH2_THETA) / (UH2_THETA ** UH2_K * math.gamma(UH2_K))


_UH2_PEAK_V = _uh2((UH2_K - 1) * UH2_THETA)


def _inflow(t: float, peak: float) -> float:
    """Reservoir inflow (m³/s) at time t hours, scaled to a target peak."""
    if peak <= BASEFLOW:
        return BASEFLOW
    return BASEFLOW + (peak - BASEFLOW) * _uh(t) / _UH_PEAK_V


def _severity(value: float, thresholds, labels=("safe", "watch", "warning", "critical")):
    """Map a value to a severity label given ascending thresholds."""
    for thr, lab in zip(thresholds, labels[1:]):
        if value < thr:
            return labels[max(0, labels.index(lab) - 1)]
    return labels[-1]


def _target_peak(rain_peak: float) -> float:
    """Reservoir inflow peak (m³/s) implied by the cloudburst rainfall volume."""
    return BASEFLOW + rain_peak * 92.0          # calibrated: 7.2 mm/hr -> ~850 m³/s


def _release_at(t: float, flood_mode: bool) -> float:
    """Actual dam release (m³/s): ramps from baseline to safe channel over ~2 h."""
    if not flood_mode:
        return BASE_RELEASE
    return BASE_RELEASE + (SAFE_CHANNEL - BASE_RELEASE) * min(1.0, max(0.0, t) / 2.0)


def _q_lat(t: float, lat_peak: float) -> float:
    """Local runoff entering the river BELOW the dam (urban flash-flood driver)."""
    if lat_peak <= 0 or t <= 0:
        return 0.0
    return lat_peak * _uh2(t) / _UH2_PEAK_V


# ============================================================================
# Forcing — the inputs that drive one simulation
# ============================================================================
# The engine is a pure function of its forcing. In DEMO mode the forcing comes
# from a canned scenario; in LIVE mode it is built from real observations
# (see live_data.py). Keeping this one seam means the physics and the whole
# dashboard are identical in both modes.
@dataclass
class Forcing:
    rain_peak: float                 # catchment-avg effective rainfall peak (mm/hr)
    heat: float                      # heatwave temperature offset above 30 °C
    dry: bool                        # dry spell -> no root-zone recharge
    label: str = "Custom"
    desc: str = ""
    source: str = "demo"             # "demo" | "live"

    # --- live-only observation overrides (ignored in demo mode) ---------
    reservoir_start_frac: float | None = None   # from the manual slider
    soil_moisture_obs: float | None = None      # real root-zone θ (m³/m³)
    et0_obs: float | None = None                # real FAO ET₀ (mm/day)
    temp_obs: float | None = None               # real 2 m temperature (°C)
    sm_days_obs: np.ndarray | None = None       # real recent soil-moisture x
    sm_series_obs: np.ndarray | None = None     # real recent soil-moisture y


def forcing_from_scenario(scenario: str) -> Forcing:
    """Build demo forcing from one of the canned SCENARIOS."""
    cfg = SCENARIOS[scenario]
    return Forcing(rain_peak=cfg["rain_peak"], heat=cfg["heat"], dry=cfg["dry"],
                   label=cfg["label"], desc=cfg["desc"], source="demo")


def forcing_from_live(obs, reservoir_start_frac: float | None = None) -> Forcing:
    """Map a live observation snapshot (live_data.LiveObs) to engine forcing.

    On a failed fetch (``obs.ok`` is False) this returns a neutral live forcing
    so the dashboard still renders a calm, clearly-labelled 'unavailable' state.
    """
    if obs is None or not getattr(obs, "ok", False):
        return Forcing(rain_peak=0.6, heat=0.0, dry=False,
                       label="Live — data unavailable", source="live",
                       reservoir_start_frac=reservoir_start_frac,
                       soil_moisture_obs=0.28, et0_obs=4.0, temp_obs=30.0)

    def _fin(v):
        return v if (v is not None and math.isfinite(v)) else None

    return Forcing(
        rain_peak=max(0.0, obs.rain_peak),
        heat=max(0.0, obs.heat),
        dry=bool(obs.dry),
        label=f"Live — {obs.source}",
        desc="Real-time observations for the selected basin.",
        source="live",
        reservoir_start_frac=reservoir_start_frac,
        soil_moisture_obs=_fin(obs.soil_moisture),
        et0_obs=_fin(obs.et0_now),
        temp_obs=_fin(obs.temp_now),
        sm_days_obs=(obs.sm_days if getattr(obs, "sm_days", None) is not None
                     and obs.sm_days.size else None),
        sm_series_obs=(obs.sm_series if getattr(obs, "sm_series", None) is not None
                       and obs.sm_series.size else None),
    )


# ============================================================================
# Place — the location labels used in the advisory text
# ============================================================================
# The physics is location-agnostic, but the plain-language directives name real
# places ("Junnar–Daund belt", "Khadakwasla Reservoir", "Sectors 4 & 5"). In
# DEMO mode those scripted names are kept exactly (DEMO_PLACE). In LIVE mode the
# labels are derived from the selected region so the advisories talk about the
# place the user picked, not Pune.
@dataclass
class Place:
    region: str          # full region label, e.g. "Nashik, Maharashtra, India"
    short: str           # short name, e.g. "Nashik"
    agri_belt: str       # farmland label, e.g. "Nashik agricultural belt"
    reservoir: str       # reservoir/dam label, e.g. "Nashik reservoir"
    dam_name: str        # feed dam label (no "Reservoir" suffix), e.g. "Khadakwasla"
    # The zone designator, bare: it names the zones and nothing else. No leading
    # article, and no "low-lying" qualifier, because the sentence around it
    # supplies that itself ("Evacuate low-lying {sectors_text} now"). Both
    # sources below must keep to that form — when one of them folded the
    # qualifier into the label, live mode read "Evacuate low-lying the low-lying
    # Nashik riverside zones", which is the fault this comment exists to prevent.
    sectors_text: str    # e.g. "Sectors 4 and 5" / "Nashik riverside zones"
    sectors_amp: str     # the same label with HTML '&amp;', for meta/feed
    road_action: str     # full action sentence: closing the riverside road

    # Evacuation geography. ``sectors`` drives the Disaster-response tab's zone
    # table and evacuation map, so no basin's zones are hard-coded in the UI.
    # Each entry: name / zone / risk (required) plus the layout hints the tab
    # needs — elev (elevation label), share (fraction of at-risk households),
    # delay (minutes added to time-to-impact), dlat/dlon (indicative offset from
    # the basin centre for the map marker).
    sectors: list[dict] = field(default_factory=lambda: [
        {"name": "Low-lying Riverfront", "zone": "Zone A", "risk": "High",
         "elev": "538 m", "share": 0.41, "delay": 0, "dlat": -0.035, "dlon": -0.045},
        {"name": "Embankment Roadway", "zone": "Zone B", "risk": "Medium",
         "elev": "540 m", "share": 0.59, "delay": 15, "dlat": -0.055, "dlon": -0.020},
        {"name": "Municipal Market Area", "zone": "Zone C", "risk": "Low",
         "elev": "544 m", "share": 0.0, "delay": 90, "dlat": -0.070, "dlon": 0.010,
         "households": 300},
    ])
    # where residents are moved to, as a sentence fragment ("Move residents to …").
    # Unlike sectors_text this one carries its own article, because both real
    # sources do ("the high-ground shelters on Route H2" / "the {short} district
    # …") and the consumer prepends nothing. This default is a fallback only —
    # DEMO_PLACE and place_from_region both set it — but it has to obey the same
    # rule, or a directly constructed Place would read "Move residents to
    # designated district emergency high ground."
    shelter_phrase: str = "the designated district emergency high ground"
    # compact label for the "Shelters ready" readout context
    shelter_short: str = "District designated high ground"
    # how the at-risk area is described; basins without a surveyed levee datum
    # get a zone-based phrase instead of an invented elevation
    risk_elev_phrase: str = "in the mapped low-lying flood zone"


# Exact scripted labels for the demo story (Upper Bhima / Pune). Every fragment
# reproduces the original hard-coded literal so demo output is byte-identical.
DEMO_PLACE = Place(
    region="Upper Bhima Basin — Pune",
    short="Junnar–Daund",           # feed line reads "Farmers, Junnar–Daund"
    agri_belt="Junnar–Daund belt",
    reservoir="Khadakwasla Reservoir",
    dam_name="Khadakwasla",
    sectors_text="Sectors 4 and 5",
    sectors_amp="Sectors 4 &amp; 5",
    road_action="Close the riverside road at the Sector 4 junction to incoming traffic.",
    sectors=[
        {"name": "Sector 4 — riverfront", "zone": "Sector 4", "risk": "High",
         "elev": "538 m", "share": 0.41, "delay": 0, "dlat": -0.035, "dlon": -0.045},
        {"name": "Sector 5 — low road", "zone": "Sector 5", "risk": "Medium",
         "elev": "540 m", "share": 0.59, "delay": 15, "dlat": -0.055, "dlon": -0.020},
        {"name": "Sector 6 — market", "zone": "Sector 6", "risk": "Low",
         "elev": "544 m", "share": 0.0, "delay": 90, "dlat": -0.070, "dlon": 0.010,
         "households": 300},
    ],
    shelter_phrase="the high-ground shelters on Route H2",
    shelter_short="On Route H2 high ground",
    risk_elev_phrase="below 542 m elevation",
)


def place_from_region(region_name: str) -> Place:
    """Derive advisory labels from a selected region name.

    ``region_name`` is whatever the picker stored (a preset like
    "Upper Bhima Basin — Pune" or a geocoded label like
    "Nashik, Maharashtra, India"). The short name is the first comma- or
    dash-separated token.

    Everything place-specific — including the evacuation zones and the shelter
    phrase — is derived here, so a non-Pune basin never inherits Pune's
    "Sector 4/5/6" or "Route H2" labels.
    """
    if not region_name:
        return DEMO_PLACE
    short = region_name.split(",")[0].split("—")[0].split("-")[0].strip() or region_name
    return Place(
        region=region_name,
        short=short,
        agri_belt=f"{short} agricultural belt",
        reservoir=f"{short} reservoir",
        dam_name=short,
        # Bare zone designator, as the Place field comment requires: the
        # "low-lying" qualifier and any article belong to the sentence that uses
        # the label, not to the label itself. There is no '&' to escape here, so
        # the two variants are identical.
        sectors_text=f"{short} riverside zones",
        sectors_amp=f"{short} riverside zones",
        road_action="Close low-lying riverside access roads to incoming traffic.",
        # Place() supplies the generic Zone A/B/C geography via its field default
        shelter_phrase=f"the {short} district designated emergency high ground",
        shelter_short=f"{short} district high ground",
    )


def live_tick_for(obs) -> int:
    """Position the engine clock ('now') from real rainfall timing.

    The reservoir inflow peaks ~``_UH_PEAK_T`` hours after the rain. Placing
    'now' at ``_UH_PEAK_T - rain_peak_in_h`` makes the live inflow forecast peak
    line up with the observed rainfall forecast, while reusing all of the
    existing inflow / reservoir / levee / gate math.
    """
    if obs is None or not getattr(obs, "ok", False) or obs.rain_peak <= 0:
        return 0
    flood_h = min(FLOOD_HORIZON_H, max(0.0, _UH_PEAK_T - obs.rain_peak_in_h))
    return int(round(TICKS_MAX * flood_h / FLOOD_HORIZON_H))


# ============================================================================
# Core simulation
# ============================================================================
def simulate(spec, tick: int, base_time: datetime | None = None) -> BasinState:
    """Compute a full basin state.

    ``base_time`` is the clock origin every timestamp in this state (and in the
    ``gate_schedule`` / ``make_directives`` derived from it) is measured from.
    LIVE mode passes the real observation time; DEMO mode leaves it ``None`` so
    the deterministic ``BASE_TIME`` story clock is used.
    """
    # measured wall-clock cost of the operational hydrology below (see
    # s.real_compute_ms). Start the timer before any physics runs.
    _t0 = time.perf_counter()

    # forcing comes from a canned scenario (demo) or real observations (live)
    forcing = spec if isinstance(spec, Forcing) else forcing_from_scenario(spec)
    scenario_key = spec if isinstance(spec, str) else forcing.source
    live = forcing.source == "live"
    tick = int(max(0, min(TICKS_MAX, tick)))
    frac = tick / TICKS_MAX

    flood_h = frac * FLOOD_HORIZON_H
    drought_d = frac * DROUGHT_HORIZON_D
    t_base = _clock_base(base_time)
    # Demo's BASE_TIME is naive and scripted in IST; a live base_time carries the
    # basin's own zone, so label the clock with that zone instead of assuming IST.
    tz_label = (t_base.strftime("%Z") or "IST") if t_base.tzinfo is not None else "IST"
    clock = (t_base + timedelta(hours=flood_h)).strftime(f"%d %b %Y, %H:%M {tz_label}")

    s = BasinState(scenario=scenario_key, tick=tick, flood_hours=flood_h,
                   drought_day=drought_d, clock=clock, base_time=t_base)
    s.start_frac = (forcing.reservoir_start_frac
                    if forcing.reservoir_start_frac is not None else RES_START_FRAC)

    # ---- flood forcing --------------------------------------------------
    # target inflow peak grows with cloudburst rainfall volume
    rain_peak = forcing.rain_peak
    heat = forcing.heat
    dry = forcing.dry
    target_peak = _target_peak(rain_peak)
    flood_mode = target_peak > SAFE_CHANNEL
    s.target_peak = target_peak
    s.flood_mode = flood_mode
    lat_peak = rain_peak * 35.0 if flood_mode else 0.0  # local below-dam runoff peak (m³/s)
    # catchment-average rainfall pulse (mm/hr), cloudburst centred at 1.5 h
    s.rain_now = rain_peak * math.exp(-((flood_h - 1.5) ** 2) / (2 * 0.7 ** 2))
    s.inflow_now = _inflow(flood_h, target_peak)

    # forecast inflow for the next 6 hours (what the operator sees "ahead")
    fc = np.linspace(0.0, 6.0, 25)                     # hours from now
    s.fc_hours = fc
    s.fc_inflow = np.array([_inflow(flood_h + dt, target_peak) for dt in fc])
    # peak of the *full* event (for headline forecast)
    full_t = np.linspace(0.0, FLOOD_HORIZON_H, 200)
    full_q = np.array([_inflow(tt, target_peak) for tt in full_t])
    ipk = int(full_q.argmax())
    s.inflow_peak = float(full_q[ipk])
    s.inflow_peak_in_h = max(0.0, float(full_t[ipk] - flood_h))
    s.past_peak = flood_mode and flood_h > full_t[ipk] + 0.05

    # ---- FIRO pre-release recommendation --------------------------------
    # volume above safe channel that must be absorbed as surcharge (acre-ft)
    surcharge = np.clip(full_q - SAFE_CHANNEL, 0, None)
    surcharge_vol = float(_trapz(surcharge, full_t) * MS_TO_AFT_PER_H)
    s.target_buffer_aft = round(surcharge_vol / 500.0) * 500.0
    # recommended controlled pre-release = safe channel capacity during a surge
    s.firo_release = SAFE_CHANNEL if flood_mode else BASE_RELEASE

    # ---- reservoir mass balance (integrate 0 -> now) --------------------
    dt = 0.1
    storage = RES_CAP_AFT * s.start_frac
    tt = 0.0
    while tt < flood_h - 1e-9:
        q_in = _inflow(tt, target_peak)
        net = (q_in - _release_at(tt, flood_mode)) * MS_TO_AFT_PER_H     # acre-ft per hour
        storage = min(RES_CAP_AFT, max(0.0, storage + net * dt))
        tt += dt
    s.storage_aft = storage
    s.reservoir_pct = 100.0 * storage / RES_CAP_AFT
    s.reservoir_level = LEVEL_MIN + (LEVEL_MAX - LEVEL_MIN) * storage / RES_CAP_AFT
    s.release_now = _release_at(flood_h, flood_mode)
    s.buffer_now_aft = RES_CAP_AFT - storage
    s.spilling = storage >= RES_CAP_AFT - 1e-6

    # ---- downstream stage / levee overtopping ---------------------------
    s.q_downstream = s.release_now + _q_lat(flood_h, lat_peak)
    # scan the next 6 h of downstream flow for the first overtopping moment
    q_ds_fc = np.array([_release_at(flood_h + d, flood_mode) + _q_lat(flood_h + d, lat_peak) for d in fc])
    over = np.where(q_ds_fc > LEVEE_Q)[0]
    if over.size:
        s.time_to_overtop_min = float(fc[over[0]] * 60.0)
    peak_q_ds = float(q_ds_fc.max())
    s.overtop_depth = max(0.0, round(0.004 * (peak_q_ds - LEVEE_Q), 2))
    if s.overtop_depth > 0.35:
        s.households_at_risk = HH_TOTAL
    elif s.overtop_depth > 0.1:
        s.households_at_risk = int(HH_TOTAL * 0.6)
    else:
        s.households_at_risk = 0

    # ---- drought : soil-moisture bucket + evaporative stress ------------
    def temp_at(d: float) -> float:
        return 30.0 + heat * min(1.0, d / 8.0)               # smooth heat ramp

    def pet_at(d: float) -> float:
        return max(1.0, 0.30 * (temp_at(d) - 5.0))           # mm/day, demand

    s.temp = temp_at(drought_d)
    s.pet = pet_at(drought_d)

    # integrate soil moisture over the drought window (daily step)
    theta = THETA_FC
    n_days = int(math.ceil(drought_d)) + 1
    days_axis = np.arange(0, n_days)
    series = []
    for d in range(n_days):
        stress = float(np.clip((theta - THETA_WP) / (THETA_FC - THETA_WP), 0, 1))
        et_act_d = pet_at(d) * stress
        # dry spell: no recharge; otherwise the monsoon keeps the root zone wet
        precip = 0.0 if dry else et_act_d + 0.5
        theta = float(np.clip(theta + (precip - et_act_d) / ROOT_DEPTH_MM, THETA_WP, THETA_FC))
        series.append(theta)
    s.sm_days = days_axis[-14:] if days_axis.size > 14 else days_axis
    s.sm_series = np.array(series)[-14:] if len(series) > 14 else np.array(series)
    s.soil_moisture = series[-1] if series else THETA_FC

    # LIVE: replace the synthetic soil state with the real observations, so the
    # drought readouts (ESR, ESP, days-to-wilting, trend chart) reflect reality.
    if live and forcing.soil_moisture_obs is not None:
        s.soil_moisture = float(forcing.soil_moisture_obs)
        if forcing.temp_obs is not None:
            s.temp = float(forcing.temp_obs)
        if forcing.et0_obs is not None:
            s.pet = max(0.1, float(forcing.et0_obs))
        if forcing.sm_series_obs is not None and forcing.sm_series_obs.size:
            s.sm_series = np.asarray(forcing.sm_series_obs, dtype=float)
            s.sm_days = (np.asarray(forcing.sm_days_obs, dtype=float)
                         if (forcing.sm_days_obs is not None
                             and forcing.sm_days_obs.size == s.sm_series.size)
                         else np.arange(s.sm_series.size, dtype=float))

    stress_now = float(np.clip((s.soil_moisture - THETA_WP) / (THETA_FC - THETA_WP), 0, 1))
    s.et_actual = s.pet * stress_now
    s.esr = stress_now                                     # ESR = ET_actual / PET
    s.esp = round(100.0 * _norm_cdf((s.esr - ESR_CLIM_MU) / ESR_CLIM_SD), 1)

    # days to wilting: project current drying rate forward
    if dry and s.soil_moisture > THETA_WP:
        dry_rate = max(1e-4, (s.et_actual) / ROOT_DEPTH_MM)   # per day
        s.days_to_wilting = round((s.soil_moisture - THETA_WP) / dry_rate, 1)
    else:
        s.days_to_wilting = float("inf")

    # ---- severities -----------------------------------------------------
    # flood: from forecast peak inflow and how soon the levee overtops
    if s.inflow_peak >= 780 or s.time_to_overtop_min <= 120:
        s.flood_sev = "critical" if s.time_to_overtop_min <= 100 else "warning"
    elif s.inflow_peak >= 450:
        s.flood_sev = "warning"
    elif s.inflow_peak >= 300:
        s.flood_sev = "watch"
    else:
        s.flood_sev = "safe"

    # drought: from the evaporative-stress percentile
    if s.esp < 10:
        s.drought_sev = "critical"
    elif s.esp < 30:
        s.drought_sev = "warning"
    elif s.esp < 50:
        s.drought_sev = "watch"
    else:
        s.drought_sev = "safe"

    # ---- measured execution time ----------------------------------------
    # The operational engine's REAL runtime: wall-clock time actually spent in
    # the physics above, in milliseconds. No synthetic value, no sleep.
    s.real_compute_ms = round((time.perf_counter() - _t0) * 1000, 2)
    # compute_time_s stays the published RESEARCH benchmark for the PINN
    # flood-map surrogate (see BENCHMARK_COMPUTE_S) — it is NOT this runtime.
    s.compute_time_s = BENCHMARK_COMPUTE_S
    return s


# ============================================================================
# Reservoir gate schedule (forward plan, integrated with the same mass balance)
# ============================================================================
def gate_schedule(s: BasinState, base_time: datetime | None = None) -> list:
    """Forward-looking gate schedule for the operations table.

    Sampled at fixed event times; the release column comes from the same
    release rule the mass balance uses, and the level column is the storage
    integrated forward with that rule — so release, level and action agree.
    Each row is tagged done / now / planned relative to the live clock.

    Row clock times are derived from ``base_time`` (LIVE: the observation time),
    falling back to the ``base_time`` stamped on ``s`` by ``simulate()``, so the
    gate clock can never disagree with the header clock.
    """
    t_base = _clock_base(base_time, s)
    target_peak = s.target_peak
    flood_mode = s.flood_mode

    if flood_mode:
        samples = [
            (0.0, "Open gates — begin FIRO pre-release"),
            (1.0, "Ramp release toward safe channel"),
            (2.0, "Hold at safe-channel limit"),
            (3.5, "Hold — absorb inflow crest as surcharge"),
            (5.0, "Hold — confirm recession"),
            (7.0, "Hold safe channel — prepare to refill"),
        ]
    else:
        samples = [
            (0.0, "Maintain normal release"),
            (2.5, "Hold normal release"),
            (5.0, "Hold normal release"),
            (7.5, "Routine monitoring only"),
        ]

    targets = [t for t, _ in samples]
    max_t = targets[-1]
    dt = 0.05
    n_steps = int(round(max_t / dt))
    storage = RES_CAP_AFT * s.start_frac
    levels: dict = {}
    next_i = 0
    for step in range(n_steps + 1):
        tt = step * dt
        while next_i < len(targets) and tt >= targets[next_i] - 1e-9:
            levels[targets[next_i]] = LEVEL_MIN + (LEVEL_MAX - LEVEL_MIN) * storage / RES_CAP_AFT
            next_i += 1
        q_in = _inflow(tt, target_peak)
        net = (q_in - _release_at(tt, flood_mode)) * MS_TO_AFT_PER_H
        storage = min(RES_CAP_AFT, max(0.0, storage + net * dt))
    while next_i < len(targets):        # any remaining samples at final storage
        levels[targets[next_i]] = LEVEL_MIN + (LEVEL_MAX - LEVEL_MIN) * storage / RES_CAP_AFT
        next_i += 1

    rows = []
    for te, action in samples:
        rel = _release_at(te, flood_mode)
        lvl = levels[te]
        if te < s.flood_hours - 0.25:
            status = "done"
        elif te <= s.flood_hours + 0.25:
            status = "now"
        else:
            status = "planned"
        rows.append({
            "time": (t_base + timedelta(hours=te)).strftime("%H:%M"),
            "action": action,
            "release": f"{rel:.0f} m³/s",
            "level": f"{lvl:.1f} m",
            "status": status,
        })
    return rows


# ============================================================================
# Directive generation  (rule-based stand-in for the Nugen alignment layer)
# ============================================================================
SEV_LABEL = {"safe": "Normal", "watch": "Watch", "warning": "Elevated", "critical": "Critical"}


def _fmt_time(minutes: float) -> str:
    if not math.isfinite(minutes):
        return "not expected"
    if minutes <= 1:
        return "now"
    if minutes >= 90:
        return f"~{minutes/60:.1f} hours"
    return f"~{int(round(minutes))} minutes"


def make_directives(s: BasinState, place: "Place | None" = None,
                    base_time: datetime | None = None) -> dict:
    """Turn the computed state into plain-language, per-stakeholder directives.

    ``place`` supplies the location labels used in the advisory text. When it is
    None (demo mode) the scripted Upper Bhima labels are used, so the demo story
    is unchanged; Live mode passes a ``Place`` derived from the chosen region.

    ``base_time`` is the clock origin for the activity-feed timestamps (LIVE: the
    observation time), falling back to the ``base_time`` stamped on ``s`` by
    ``simulate()`` and finally to the demo ``BASE_TIME``.
    """
    if place is None:
        place = DEMO_PLACE

    # ---- farmer ---------------------------------------------------------
    if s.drought_sev in ("warning", "critical"):
        farmer = {
            "severity": s.drought_sev,
            "title": "Start pre-dawn drip irrigation tomorrow",
            "situation": (
                f"Soil in the {place.agri_belt} is at the {s.esp:.0f}th percentile of "
                f"evaporative stress and will reach the wilting point in about "
                f"{s.days_to_wilting:.0f} days. Rapid evaporation is pulling moisture from "
                f"the root zone faster than the crop can recover on its own."),
            "actions": [
                "Run drip irrigation for <b>45 minutes at 4:00 AM</b>, before the day heats up.",
                "Water fields with flowering or fruiting crops first.",
                "Avoid midday watering — most of it evaporates before roots can use it.",
            ],
            "meta": f"Warning issued {s.lead_time_days} days ahead of visible wilting — {place.agri_belt}",
            "cert": "Aligned to IMD advisory protocol",
        }
    else:
        farmer = {
            "severity": "safe",
            "title": "No irrigation action needed",
            "situation": (
                f"Root-zone moisture is healthy (evaporative stress at the {s.esp:.0f}th "
                f"percentile). Continue normal scheduling and re-check tomorrow."),
            "actions": [
                "Keep to your normal irrigation schedule.",
                "Watch the evaporative-stress reading over the next few days.",
            ],
            "meta": f"{place.agri_belt} — conditions normal",
            "cert": "Aligned to IMD advisory protocol",
        }

    # ---- dam operator ---------------------------------------------------
    if s.firo_release > BASE_RELEASE + 5 and s.past_peak:
        dam = {
            "severity": "watch",
            "title": "Hold safe-channel release while storage recovers",
            "situation": (
                f"The inflow surge has peaked at {s.inflow_peak:.0f} m³/s and is now receding. "
                f"Storage is steady at {s.reservoir_pct:.0f}% — the pre-release created enough "
                f"buffer to absorb the crest without an emergency spill."),
            "actions": [
                f"Keep release at the safe channel limit of {SAFE_CHANNEL:.0f} m³/s until inflow drops below it.",
                "Begin refilling to the normal rule curve once the recession is confirmed.",
                "Log the surcharge volume used against the FIRO forecast.",
            ],
            "meta": f"Post-crest recovery — {place.reservoir}",
            "cert": "Certified against CWC operation manual",
        }
    elif s.firo_release > BASE_RELEASE + 5:
        dam = {
            "severity": s.flood_sev if s.flood_sev != "safe" else "watch",
            "title": f"Pre-release {s.firo_release:.0f} m³/s now to open flood buffer",
            "situation": (
                f"A cloudburst upstream is expected to push inflow to {s.inflow_peak:.0f} m³/s "
                f"within {s.inflow_peak_in_h:.1f} hours. A controlled release now creates room "
                f"to absorb the surge and avoids an emergency spill later."),
            "actions": [
                f"Open gates to a controlled <b>{s.firo_release:.0f} m³/s</b> pre-release immediately.",
                f"Target a <b>{s.target_buffer_aft:,.0f} acre-foot</b> buffer before the flood crest arrives.",
                f"Keep downstream release within the safe channel capacity of {SAFE_CHANNEL:.0f} m³/s.",
            ],
            "meta": f"Inflow surge expected in {s.inflow_peak_in_h:.1f} hours — {place.reservoir}",
            "cert": "Certified against CWC operation manual",
        }
    elif s.flood_sev == "watch":
        # Inflow is elevated (Watch band) but still below the pre-release
        # trigger, so the reservoir plan is "hold and stage". Without this
        # branch the basin reported "no directives active" while the flood
        # panel showed Watch — the two contradicted each other.
        # Only this branch carries the extra CWC advisory fields; readers must
        # use .get() so the other branches (and the frozen demo output) are
        # unaffected.
        dam = {
            "severity": "watch",
            "role": "Dam Operations",
            "urgency": "Advisory",
            "action_code": "CWC-WATCH-STAGE1",
            "title": f"Stage-1 watch — hold normal release, ready {place.dam_name} gates",
            "situation": (
                f"An inflow surge is building toward {s.inflow_peak:.0f} m³/s "
                f"(peak in about {s.inflow_peak_in_h:.1f} hours) — above normal but still "
                f"within the safe channel capacity of {SAFE_CHANNEL:.0f} m³/s. "
                f"{place.reservoir} holds its normal release for now, with the radial gates "
                f"and the spillway readied so a pre-release can start without delay if the "
                f"forecast climbs."),
            "actions": [
                f"Hold the normal release of {BASE_RELEASE:.0f} m³/s — no pre-release yet.",
                f"Place the {place.dam_name} radial gates on Stage-1 standby and confirm "
                f"hoist power and manual override.",
                "Clear the spillway channel and confirm downstream gauges are reporting.",
                "Re-run the inflow forecast each hour; escalate to FIRO pre-release if the "
                f"peak forecast exceeds {SAFE_CHANNEL:.0f} m³/s.",
            ],
            "meta": f"Stage-1 watch — {place.reservoir}",
            "cert": "Certified against CWC operation manual",
        }
    else:
        dam = {
            "severity": "safe",
            "title": "Hold normal reservoir operation",
            "situation": (
                f"Inflow is near baseline ({s.inflow_now:.0f} m³/s) and storage is steady at "
                f"{s.reservoir_pct:.0f}%. No pre-release is required."),
            "actions": [
                f"Maintain the normal release of {BASE_RELEASE:.0f} m³/s.",
                "Keep monitoring upstream rainfall for any change.",
            ],
            "meta": f"{place.reservoir} — steady state",
            "cert": "Certified against CWC operation manual",
        }

    # ---- disaster team --------------------------------------------------
    if math.isfinite(s.time_to_overtop_min) and s.overtop_depth > 0.1:
        imminent = s.time_to_overtop_min <= 1
        title = (f"Evacuate low-lying {place.sectors_text} now — levee overtopping"
                 if imminent else
                 f"Evacuate low-lying {place.sectors_text} within {int(round(s.time_to_overtop_min))} minutes")
        disaster = {
            "severity": "critical" if s.time_to_overtop_min <= 100 else "warning",
            "title": title,
            "situation": (
                f"The river will rise about {s.overtop_depth:.1f} m above the levee crest at "
                f"{place.sectors_text} ({_fmt_time(s.time_to_overtop_min)}). "
                f"Areas {place.risk_elev_phrase} are at risk of inundation."),
            "actions": [
                f"Begin geofenced evacuation for all zones <b>{place.risk_elev_phrase}</b>.",
                f"Move residents to {place.shelter_phrase}.",
                place.road_action,
            ],
            "meta": f"Impact {_fmt_time(s.time_to_overtop_min)} — {place.sectors_amp}",
            "cert": "Aligned to district disaster-management SOP",
        }
    else:
        disaster = {
            "severity": "safe",
            "title": "No evacuation required",
            "situation": (
                "The river is well within the levee. Riverside sectors are not at risk under "
                "the current forecast."),
            "actions": [
                "Maintain routine monitoring of river-gauge levels.",
                "Keep shelters on standby but no action needed.",
            ],
            "meta": "Riverside sectors — within safe levels",
            "cert": "Aligned to district disaster-management SOP",
        }

    # ---- SMS body -------------------------------------------------------
    # Mirrors the farmer directive above: an irrigation alert only goes out
    # when the drought state is a warning/critical. Otherwise farmers receive
    # an all-clear so the SMS never contradicts the on-screen "Normal" status.
    if s.drought_sev in ("warning", "critical"):
        sms = {
            "mr": ("सावधान: पुढील २४ तासांत तीव्र बाष्पीभवन अपेक्षित आहे. पिकांचे नुकसान "
                   "टाळण्यासाठी उद्या पहाटे ४:०० वाजता ४५ मिनिटे ठिबक सिंचन सुरू करा."),
            "en": ("Alert: Intense evaporation is expected in the next 24 hours. To protect your "
                   "crop, run drip irrigation for 45 minutes tomorrow at 4:00 AM."),
            "sent": 1240,
        }
    else:
        sms = {
            "mr": ("माहिती: जमिनीतील ओलावा सध्या पुरेसा आहे. सिंचनाची गरज नाही — नेहमीचे "
                   "वेळापत्रक सुरू ठेवा. उद्या पुन्हा तपासा."),
            "en": ("Update: Soil moisture is currently adequate. No irrigation needed — keep to "
                   "your normal schedule. We'll re-check tomorrow."),
            "sent": 1240,
        }

    # ---- overview activity feed ----------------------------------------
    feed = []
    base = _clock_base(base_time, s) + timedelta(hours=s.flood_hours)
    if disaster["severity"] != "safe":
        feed.append((base.strftime("%H:%M"), disaster["severity"],
                     f"<b>{place.sectors_amp}:</b> prepare geofenced evacuation, "
                     f"impact expected {_fmt_time(s.time_to_overtop_min)}."))
    if s.firo_release > BASE_RELEASE + 5 and not s.past_peak:
        feed.append(((base - timedelta(minutes=18)).strftime("%H:%M"), dam["severity"],
                     f"<b>{place.dam_name} dam:</b> begin {s.firo_release:.0f} m³/s controlled pre-release."))
    elif s.firo_release > BASE_RELEASE + 5 and s.past_peak:
        feed.append(((base - timedelta(minutes=18)).strftime("%H:%M"), "watch",
                     f"<b>{place.dam_name} dam:</b> surge crest absorbed — holding safe-channel release."))
    elif dam.get("action_code") == "CWC-WATCH-STAGE1":
        # Stage-1 watch advisory: issued 15 minutes after the clock base, so an
        # elevated inflow never shows up as "no directives active".
        feed.append(((base + timedelta(minutes=15)).strftime("%H:%M"), "watch",
                     f"<b>{place.dam_name} dam:</b> Stage-1 watch — inflow surge to "
                     f"{s.inflow_peak:.0f} m³/s, radial gates on standby, holding normal release."))
    if farmer["severity"] in ("warning", "critical"):
        feed.append(((base - timedelta(minutes=32)).strftime("%H:%M"), farmer["severity"],
                     f"<b>Farmers, {place.short}:</b> start pre-dawn drip irrigation to protect roots."))
    if not feed:
        feed.append((base.strftime("%H:%M"), "safe",
                     "<b>Basin normal:</b> no directives active. Continuing routine monitoring."))

    return {"farmer": farmer, "dam": dam, "disaster": disaster, "sms": sms, "feed": feed}


# ============================================================================
# RESPONSE SUPPORT — the deterministic backing for the AI assistance layer
# ============================================================================
# Three things the AI is allowed to help with, and the computed candidate set
# behind each one. The division is the whole point: the engine decides WHETHER
# (above, in simulate and make_directives) and this block decides WHAT CAN BE
# DONE ABOUT IT. The language layer may phrase and prioritise what it finds
# here; it may not add a measure, name a place, or state a hazard.
#
# Everything below is additive. simulate() and make_directives() do not call
# into it and their return values are untouched, so the frozen demo snapshot
# cannot move.
# ============================================================================

_SEV_RANK = {"critical": 0, "warning": 1, "watch": 2, "safe": 3}


def _tier(value: float, watch: float, warning: float, critical: float) -> str:
    """Severity from ascending cuts. Negate the inputs for a descending scale."""
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    if value >= watch:
        return "watch"
    return "safe"


def _plural(n: float, one: str, many: str) -> str:
    return one if abs(n - 1) < 1e-9 else many


# ---------------------------------------------------------------------------
# 1. PATTERNS — what the computed series already shows
#
# Every detail string is a statement in the past or present tense about data
# that exists now. None says "will", "expected", "likely" or names a future
# event, because a pattern is evidence and a forecast is a claim. Where a
# detector reads a forward-looking array (fc_inflow holds the next six hours),
# the sentence is phrased as a property of the stored curve rather than as a
# prediction about the river.
#
# Thresholds reuse the engine's own published constants wherever one exists, so
# a pattern can never disagree with the severity badge beside it.
# ---------------------------------------------------------------------------
# Tie-break inside a severity band: operational urgency, flood before drought.
_PATTERN_PRIORITY = {
    "levee_threshold_crossed": 0,
    "downstream_near_channel_limit": 1,
    "inflow_curve_rising": 2,
    "storage_accumulating": 3,
    "compound_flood_drought": 4,
    "esp_climatology_tail": 5,
    "et_suppressed_below_pet": 6,
    "soil_moisture_declining": 7,
    "rain_without_recharge": 8,
    "no_rainfall_input": 9,
    "inflow_receding": 10,
    "surcharge_headroom": 11,
    "firo_buffer_opening": 12,
    "soil_moisture_holding": 13,
}


@dataclass
class Pattern:
    """One thing the computed data shows, with the arithmetic behind it."""
    key: str            # stable id
    label: str          # short human label
    detail: str         # the evidence, with numbers, past or present tense
    severity: str       # safe | watch | warning | critical
    basis: str          # the state fields it was computed from
    audience: str = "all"   # all | farmer | dam | disaster


def detect_patterns(s: BasinState) -> list:
    """Deterministic patterns in the computed series. Most severe first.

    No model runs here and none is consulted. Each entry is arithmetic over a
    BasinState that simulate() already produced, which is what makes it safe to
    put in front of an operator: a pattern is reproducible, a prediction is not.
    """
    out: list = []
    fc = np.asarray(s.fc_inflow, dtype=float)
    fch = np.asarray(s.fc_hours, dtype=float)
    sm = np.asarray(s.sm_series, dtype=float)

    # -- inflow curve climbing steeply -----------------------------------
    if fc.size >= 13 and fch.size == fc.size:
        i3 = 12                               # fc_hours[12] is +3 h
        d3 = float(fc[i3] - fc[0])
        h3 = float(fch[i3] - fch[0]) or 3.0
        if d3 >= 60.0:        # above the calm-basin envelope (normal peaks at 49)
            out.append(Pattern(
                "inflow_curve_rising", "Inflow curve climbing steeply",
                f"The computed inflow curve rises {d3:.0f} m³/s across its first "
                f"{h3:.0f} hours, from {fc[0]:.0f} to {fc[i3]:.0f} m³/s "
                f"({d3 / h3:.0f} m³/s per hour).",
                _tier(d3, 60, 200, 500), "fc_inflow, fc_hours", "dam"))

    # -- the crest is behind us ------------------------------------------
    if fc.size >= 2:
        drop = float(fc[0] - fc[-1])
        if int(fc.argmax()) == 0 and drop >= 60.0:
            tail = " The engine's past-peak flag is set." if s.past_peak else ""
            out.append(Pattern(
                "inflow_receding", "Inflow crest has passed",
                f"The highest ordinate in the computed inflow curve is the present "
                f"value of {fc[0]:.0f} m³/s, and the curve falls {drop:.0f} m³/s "
                f"across the stored {float(fch[-1]):.0f}-hour window.{tail}",
                "watch" if fc[0] > SAFE_CHANNEL else "safe",
                "fc_inflow, past_peak", "dam"))

    # -- release below inflow, so storage is accumulating ----------------
    surplus = s.inflow_now - s.release_now
    if surplus >= BASE_RELEASE:    # gaining a full normal release per hour or more
        net = surplus * MS_TO_AFT_PER_H
        hrs = s.buffer_now_aft / net if net > 0 else float("inf")
        sev = _tier(surplus, BASE_RELEASE, 2 * BASE_RELEASE,
                    SAFE_CHANNEL - BASE_RELEASE)
        if s.spilling:
            sev = "critical"
        out.append(Pattern(
            "storage_accumulating", "Release below inflow, storage rising",
            f"Release is {s.release_now:.0f} m³/s against an inflow of "
            f"{s.inflow_now:.0f} m³/s, a surplus of {surplus:.0f} m³/s, so storage "
            f"gains {net:.0f} acre-ft per hour; the {s.buffer_now_aft:,.0f} "
            f"acre-ft of headroom left at {s.reservoir_pct:.1f}% full covers "
            f"{hrs:.0f} hours of that surplus.",
            sev, "inflow_now, release_now, buffer_now_aft, spilling", "dam"))

    # -- pre-release is opening buffer (the good-news case) --------------
    if surplus < -5.0:          # -5 m³/s deadband so a near balance cannot flicker
        draw = -surplus * MS_TO_AFT_PER_H
        out.append(Pattern(
            "firo_buffer_opening", "Pre-release is opening buffer",
            f"Release of {s.release_now:.0f} m³/s exceeds inflow of "
            f"{s.inflow_now:.0f} m³/s by {-surplus:.0f} m³/s, so storage is drawing "
            f"down {draw:.0f} acre-ft per hour and headroom has grown to "
            f"{s.buffer_now_aft:,.0f} acre-ft.",
            "safe", "release_now, inflow_now, buffer_now_aft", "dam"))

    # -- downstream flow against the two published limits ----------------
    if s.q_downstream >= 0.75 * SAFE_CHANNEL:
        pct = 100.0 * s.q_downstream / SAFE_CHANNEL
        side = "above" if s.q_downstream > LEVEE_Q else "below"
        out.append(Pattern(
            "downstream_near_channel_limit", "Downstream flow at the channel limit",
            f"Downstream flow is {s.q_downstream:.0f} m³/s, {pct:.0f}% of the "
            f"{SAFE_CHANNEL:.0f} m³/s safe channel capacity and "
            f"{abs(s.q_downstream - LEVEE_Q):.0f} m³/s {side} the {LEVEE_Q:.0f} "
            f"m³/s levee threshold.",
            _tier(s.q_downstream, 0.75 * SAFE_CHANNEL, SAFE_CHANNEL, LEVEE_Q),
            "q_downstream", "disaster"))

    # -- the levee threshold is crossed inside the stored curve ----------
    if s.overtop_depth > 0.0:
        if not math.isfinite(s.time_to_overtop_min):
            when = ""
        elif s.time_to_overtop_min < 1.0:
            when = (" The curve is already above that threshold at its present "
                    "ordinate.")
        else:
            when = (f" The first ordinate above that threshold sits "
                    f"{s.time_to_overtop_min:.0f} min along the stored curve.")
        out.append(Pattern(
            "levee_threshold_crossed",
            "Levee threshold crossed in the computed curve",
            f"The computed downstream-flow curve peaks above the {LEVEE_Q:.0f} m³/s "
            f"levee threshold; the stage computed from that peak stands "
            f"{s.overtop_depth:.2f} m over the crest, which places "
            f"{s.households_at_risk:,} of {HH_TOTAL:,} households inside the mapped "
            f"low-lying zone.{when}",
            # 0.1 / 0.35 m mirror simulate()'s own households_at_risk cuts, so this
            # severity and that household count can never contradict each other.
            "critical" if s.overtop_depth > 0.35
            else ("warning" if s.overtop_depth > 0.1 else "watch"),
            "overtop_depth, time_to_overtop_min, households_at_risk", "disaster"))

    # -- the soil-moisture trajectory ------------------------------------
    drying = False
    if sm.size >= 2:              # sm_series holds a single point at tick 0
        mono = bool(np.all(np.diff(sm) <= 1e-12))
        total = float(sm[0] - sm[-1])
        span = float(sm.size - 1)
        days = np.asarray(s.sm_days, dtype=float)
        if days.size == sm.size and float(days[-1] - days[0]) > 0:
            span = float(days[-1] - days[0])
        # 0.002 m³/m³ is 1.2 mm over the root zone: above float noise, below any
        # signal an agronomist would act on.
        drying = mono and total >= 0.002
        if drying:
            frac = total / (THETA_FC - THETA_WP)
            steps = sm.size - 1
            out.append(Pattern(
                "soil_moisture_declining", "Soil moisture falling at every step",
                f"Root-zone moisture has fallen at every one of the {steps} "
                f"{_plural(steps, 'step', 'steps')} in the stored trajectory, from "
                f"{sm[0]:.3f} to {sm[-1]:.3f} m³/m³ over {span:.0f} "
                f"{_plural(span, 'day', 'days')} ({total * ROOT_DEPTH_MM:.0f} mm of "
                f"water, {100 * frac:.0f}% of the plant-available range).",
                _tier(frac, 0.0, 0.20, 0.40), "sm_series, sm_days", "farmer"))
        elif mono and s.soil_moisture >= 0.95 * THETA_FC:
            gap = (THETA_FC - s.soil_moisture) * ROOT_DEPTH_MM
            where = "exactly at" if gap < 0.5 else f"within {gap:.0f} mm of"
            out.append(Pattern(
                "soil_moisture_holding", "Soil moisture holding at field capacity",
                f"Root-zone moisture has held at {s.soil_moisture:.3f} m³/m³ across "
                f"all {sm.size} points of the stored trajectory, {where} the "
                f"{THETA_FC:.2f} m³/m³ field capacity.",
                "safe", "sm_series, soil_moisture", "farmer"))

    # -- actual water use suppressed below demand ------------------------
    deficit = s.pet - s.et_actual
    if deficit >= 1.5 and s.pet > 0:    # 1.5 mm/day is 20% of the baseline PET
        out.append(Pattern(
            "et_suppressed_below_pet", "Actual water use suppressed below demand",
            f"Actual evapotranspiration is {s.et_actual:.1f} mm/day against "
            f"atmospheric demand of {s.pet:.1f} mm/day, a shortfall of "
            f"{deficit:.1f} mm/day, so the soil is meeting only "
            f"{100.0 * s.et_actual / s.pet:.0f}% of demand at {s.temp:.1f} °C.",
            _tier(deficit, 1.5, 3.0, 5.0), "et_actual, pet, temp", "farmer"))

    # -- evaporative stress in the climatological dry tail ---------------
    if s.esp < 50.0 and s.pet > 0:      # cuts mirror simulate()'s drought ladder
        out.append(Pattern(
            "esp_climatology_tail", "Evaporative stress in the dry tail",
            f"The evaporative stress ratio of {s.esr:.2f} sits at the {s.esp:.0f}th "
            f"percentile of the basin climatology (mean {ESR_CLIM_MU:.2f}, sd "
            f"{ESR_CLIM_SD:.2f}), drier than {100.0 - s.esp:.0f}% of the historical "
            f"record.",
            _tier(-s.esp, -50.0, -30.0, -10.0), "esp, esr", "farmer"))

    # -- both hazards elevated in one state ------------------------------
    if s.flood_sev != "safe" and s.drought_sev != "safe":
        out.append(Pattern(
            "compound_flood_drought", "Both hazards elevated in the same state",
            f"The flood severity computed from the inflow peak of "
            f"{s.inflow_peak:.0f} m³/s reads '{s.flood_sev}' while the drought "
            f"severity computed from the {s.esp:.0f}th-percentile stress reading "
            f"reads '{s.drought_sev}' — one basin, both hazards above normal in the "
            f"same computed state.",
            "critical" if "critical" in (s.flood_sev, s.drought_sev) else "warning",
            "flood_sev, drought_sev, inflow_peak, esp"))

    # -- rainfall against recharge (two mutually exclusive keys) ---------
    if drying and s.rain_now >= 0.5:
        out.append(Pattern(
            "rain_without_recharge", "Rain falling while the soil keeps drying",
            f"Catchment rainfall is {s.rain_now:.1f} mm/hr, yet root-zone moisture "
            f"fell at every step of the stored trajectory to {s.soil_moisture:.3f} "
            f"m³/m³ — the runoff reaching the reservoir ({s.inflow_now:.0f} m³/s) "
            f"has not recharged the root zone.",
            "warning", "rain_now, sm_series, inflow_now", "farmer"))
    elif drying and s.rain_now < 0.15:  # below a tipping-bucket gauge's resolution
        out.append(Pattern(
            "no_rainfall_input", "No rainfall input while the soil dries",
            f"Catchment rainfall reads {s.rain_now:.2f} mm/hr with root-zone "
            f"moisture down to {s.soil_moisture:.3f} m³/m³ — the root zone has taken "
            f"no recharge across the stored {sm.size}-point trajectory.",
            "warning" if s.esp < 30 else "watch",
            "rain_now, sm_series, soil_moisture", "farmer"))

    # -- headroom against the surcharge the hydrograph requires ----------
    # The mass-balance statement, as a ratio of two computed volumes. It never
    # claims a spill happens; it reports how many times over the room exists.
    if s.target_buffer_aft > 0 and (not s.past_peak
                                    or s.reservoir_pct >= 85.0 or s.spilling):
        ratio = s.buffer_now_aft / s.target_buffer_aft
        sev = ("critical" if (s.spilling or ratio < 1.0) else
               "warning" if ratio < 2.0 else
               "watch" if ratio < 4.0 else "safe")
        out.append(Pattern(
            "surcharge_headroom", "Headroom against the required surcharge",
            f"The reservoir holds {s.buffer_now_aft:,.0f} acre-ft of headroom at "
            f"{s.reservoir_pct:.1f}% full against the {s.target_buffer_aft:,.0f} "
            f"acre-ft the computed inflow hydrograph puts above the "
            f"{SAFE_CHANNEL:.0f} m³/s safe channel — {ratio:.1f} times the volume "
            f"required.",
            sev, "buffer_now_aft, target_buffer_aft, reservoir_pct", "dam"))

    out.sort(key=lambda p: (_SEV_RANK.get(p.severity, 9),
                            _PATTERN_PRIORITY.get(p.key, 99)))
    return out


# ---------------------------------------------------------------------------
# 2. MITIGATION — measures the AI may suggest, and nothing else
#
# A catalogue, not a generator. The AI selects from this table and phrases what
# it selected; it cannot add a measure, because a measure that is not in the
# table is not in the candidate set the UI renders. That is the whole guarantee.
#
# Every `trigger` is a predicate over a BasinState the engine already computed,
# so a measure appears exactly when its condition holds and an operator can
# always be told which reading put it on screen.
#
# On `authority`: these name the family of guidance a measure is drawn from —
# the issuing body and the subject — not a clause number, because a clause
# reference this project has not verified would be worse than none. They are
# citations in the same sense as the directive cards' "Aligned to IMD advisory
# protocol": they say where the practice comes from, and they are not an
# external certification of this software. Measures marked "general practice"
# are standard control-room or field practice for which no single guideline is
# cited here.
#
# Wording rule, enforced by review: every `action` is an instruction about what
# to DO. None states or forecasts a hazard. The engine does that, above.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mitigation:
    key: str            # stable id
    role: str           # farmer | dam | disaster | public
    hazard: str         # flood | drought | both
    action: str         # the measure, imperative, one sentence
    horizon: str        # now | next 6 hours | next 24 hours | this week
    because: str        # the reading that put it on screen
    authority: str      # the guidance family it is drawn from


# Predicates are kept beside the table rather than inlined so the table stays
# readable and each condition can be checked against one engine field.
def _flood_live(s) -> bool:
    return s.flood_sev in ("watch", "warning", "critical")


def _flood_bad(s) -> bool:
    return s.flood_sev in ("warning", "critical")


def _dry_live(s) -> bool:
    return s.drought_sev in ("watch", "warning", "critical")


def _dry_bad(s) -> bool:
    return s.drought_sev in ("warning", "critical")


MITIGATIONS: tuple = (
    # ---- farmer, drought ------------------------------------------------
    Mitigation("mulch_bare_soil", "farmer", "drought",
               "Mulch the rows you are not watering — covered soil holds what "
               "it has, bare soil gives it to the air.",
               "this week", "root-zone moisture is falling on every reading",
               "ICAR drought contingency planning"),
    Mitigation("stage_priority", "farmer", "drought",
               "Water the flowering and fruiting plots first; a plot at a "
               "vegetative stage recovers from a missed turn, a flowering one "
               "does not.",
               "now", "evaporative stress is in the dry tail",
               "ICAR drought contingency planning"),
    Mitigation("defer_topdress", "farmer", "drought",
               "Hold the next fertiliser top-dressing until the crop is "
               "drinking again — salts concentrate in a dry root zone.",
               "this week", "actual water use is suppressed below demand",
               "ICAR drought contingency planning"),
    Mitigation("channel_turns", "farmer", "drought",
               "Agree a turn order with the other plots on your channel so the "
               "belt draws the water down in sequence rather than all at once.",
               "this week", "evaporative stress is in the dry tail",
               "general practice"),
    Mitigation("livestock_water", "farmer", "drought",
               "Fill and cover the livestock trough while the channel is still "
               "running, and check the fodder stock against the days you have.",
               "next 24 hours", "root-zone moisture is falling on every reading",
               "NDMA guidelines on drought management"),
    Mitigation("defer_sowing", "farmer", "drought",
               "Hold back any remaining sowing until the root zone has taken "
               "recharge; seed in dry soil is seed spent twice.",
               "this week", "the root zone has taken no recharge",
               "ICAR drought contingency planning"),
    Mitigation("check_drip", "farmer", "drought",
               "Clear the drip filters and the field channel now — a blocked "
               "line is only ever discovered when it is needed.",
               "this week", "the drought side is quiet enough to do maintenance",
               "general practice"),
    # ---- farmer, flood --------------------------------------------------
    Mitigation("field_drainage", "farmer", "flood",
               "Open the field drains and clear the outfall so standing water "
               "has somewhere to go.",
               "now", "downstream flow is at the channel limit",
               "NDMA guidelines on flood management"),
    Mitigation("lift_pumpset", "farmer", "flood",
               "Lift the pumpset, motor and any stored fertiliser off the floor "
               "and above the bank line.",
               "now", "the levee threshold is crossed in the computed curve",
               "general practice"),
    Mitigation("harvest_ahead", "farmer", "flood",
               "Bring in whatever is ready to cut, even under-ripe — a part "
               "harvest beats a submerged one.",
               "next 6 hours", "the levee threshold is crossed in the computed curve",
               "NDMA guidelines on flood management"),
    Mitigation("move_livestock_high", "farmer", "flood",
               "Move livestock to the highest ground on the zone list before "
               "the access road is cut, not after.",
               "now", "households are inside the mapped low-lying zone",
               "NDMA guidelines on flood management"),
    # ---- dam, flood -----------------------------------------------------
    Mitigation("warn_before_gate", "dam", "flood",
               "Sound the downstream siren line and send the village roster its "
               "message before the first gate moves, not with it.",
               "now", "a release change is scheduled",
               "CWC reservoir operation practice"),
    Mitigation("gate_standby", "dam", "flood",
               "Put the radial gates on standby and prove the hoist power and "
               "the manual override on each one.",
               "now", "the inflow curve is climbing steeply",
               "Standard dam-safety operating practice"),
    Mitigation("clear_spillway", "dam", "flood",
               "Walk and clear the spillway channel and the approach of debris "
               "and vegetation.",
               "next 6 hours", "the inflow curve is climbing steeply",
               "Standard dam-safety operating practice"),
    Mitigation("confirm_gauges", "dam", "flood",
               "Confirm every downstream gauge is reporting before you rely on "
               "it to tell you the release landed.",
               "now", "downstream flow is at the channel limit",
               "CWC reservoir operation practice"),
    Mitigation("second_operator", "dam", "flood",
               "Put a second operator on the far-bank radio for the whole "
               "release, so a gate reading is never one person's word.",
               "now", "a release change is scheduled",
               "general practice"),
    Mitigation("walk_channel", "dam", "flood",
               "Walk the channel, the sandbars and the pumping-station apron "
               "between the dam and the first settlement before any gate moves.",
               "next 6 hours", "downstream flow is at the channel limit",
               "general practice"),
    Mitigation("photo_tailrace", "dam", "flood",
               "Photograph the tailrace and the first bridge pier before the "
               "release and again after — it is the only before-and-after "
               "anyone will have.",
               "now", "a release change is scheduled",
               "general practice"),
    Mitigation("log_surcharge", "dam", "flood",
               "Log the surcharge volume and the gate times against the rule "
               "curve as you go; a reconstructed log is not a log.",
               "now", "storage is rising against the required surcharge",
               "CWC reservoir operation practice"),
    Mitigation("inspect_freeboard", "dam", "flood",
               "Inspect the freeboard and the upstream face for seepage or "
               "settlement while the level is high.",
               "next 24 hours", "storage is rising against the required surcharge",
               "Standard dam-safety operating practice"),
    # ---- dam, drought ---------------------------------------------------
    Mitigation("ration_release", "dam", "drought",
               "Set the release against the committed downstream demand for the "
               "whole dry spell rather than week by week.",
               "this week", "evaporative stress is in the dry tail",
               "CWC reservoir operation practice"),
    Mitigation("audit_losses", "dam", "drought",
               "Audit conveyance losses on the main canal — in a dry spell the "
               "cheapest water available is the water already being lost.",
               "this week", "evaporative stress is in the dry tail",
               "general practice"),
    # ---- disaster, flood ------------------------------------------------
    Mitigation("vulnerable_first", "disaster", "flood",
               "Start with the people who cannot start themselves — the ward's "
               "bed-bound list, the anganwadi roll, anyone without a vehicle. "
               "They set the clock, not the fit.",
               "now", "households are inside the mapped low-lying zone",
               "NDMA guidelines on incident response"),
    Mitigation("cut_power", "disaster", "flood",
               "Cut power and isolate the pumping station in the lowest zone "
               "before the water, not after; a live line in standing water "
               "closes the street to the rescue too.",
               "now", "the levee threshold is crossed in the computed curve",
               "NDMA guidelines on flood management"),
    Mitigation("back_marker", "disaster", "flood",
               "Send a back-marker through each street behind the main move and "
               "mark the door, so \"cleared\" means someone looked.",
               "now", "households are inside the mapped low-lying zone",
               "NDMA guidelines on incident response"),
    Mitigation("stage_boats", "disaster", "flood",
               "Pre-position boats and rescue crews on the high-ground side of "
               "the route, not inside the zone they will be recovering from.",
               "now", "downstream flow is at the channel limit",
               "NDMA guidelines on flood management"),
    Mitigation("close_road", "disaster", "flood",
               "Close and physically block the riverside access road — a sign "
               "is not a closure once a driver can see water on both sides.",
               "now", "the levee threshold is crossed in the computed curve",
               "NDMA guidelines on flood management"),
    Mitigation("shelter_provision", "disaster", "flood",
               "Check the assembly points your district plan designates have "
               "drinking water, light and a working generator before anyone "
               "arrives at them.",
               "next 6 hours", "an evacuation is in the directive above",
               "NDMA guidelines on incident response"),
    Mitigation("livestock_route", "disaster", "flood",
               "Move livestock ahead of people where a route is shared — they "
               "move slowest and they block what follows.",
               "now", "households are inside the mapped low-lying zone",
               "NDMA guidelines on flood management"),
    Mitigation("time_the_route", "disaster", "flood",
               "Drive the evacuation route once and time it; the number you "
               "will want later is the one nobody has written down.",
               "this week", "the flood side is quiet enough to rehearse",
               "general practice"),
    # ---- disaster, drought ----------------------------------------------
    Mitigation("water_tankers", "disaster", "drought",
               "Confirm the tanker schedule and the source for the habitations "
               "that run dry first, before the requests arrive.",
               "next 24 hours", "evaporative stress is in the dry tail",
               "NDMA guidelines on drought management"),
    Mitigation("fodder_depot", "disaster", "drought",
               "Pre-position fodder and drinking water at the cattle camps "
               "rather than waiting for the camps to ask.",
               "this week", "root-zone moisture is falling on every reading",
               "NDMA guidelines on drought management"),
    # ---- public, both ---------------------------------------------------
    Mitigation("no_moving_water", "public", "flood",
               "Do not walk or drive through moving water, whatever its depth — "
               "a foot of it carries a car.",
               "now", "households are inside the mapped low-lying zone",
               "NDMA guidelines on flood management"),
    Mitigation("documents_meds", "public", "flood",
               "Put documents, medicines and a phone charger in one bag you can "
               "carry in one hand.",
               "now", "an evacuation is in the directive above",
               "NDMA guidelines on incident response"),
    Mitigation("boil_water", "public", "flood",
               "Boil or treat drinking water once the river has been over the "
               "bank; the supply is the second hazard.",
               "next 24 hours", "the levee threshold is crossed in the computed curve",
               "NDMA guidelines on flood management"),
    Mitigation("store_water", "public", "drought",
               "Store drinking water in covered containers while the supply is "
               "still on its normal schedule.",
               "this week", "evaporative stress is in the dry tail",
               "NDMA guidelines on drought management"),
)

# key -> predicate. Kept separate from the frozen dataclass so the table stays
# data and the conditions stay code.
_MIT_TRIGGER = {
    "mulch_bare_soil": _dry_live,
    "stage_priority": _dry_bad,
    "defer_topdress": _dry_bad,
    "channel_turns": _dry_live,
    "livestock_water": _dry_bad,
    "defer_sowing": lambda s: _dry_bad(s) and s.rain_now < 0.15,
    "check_drip": lambda s: not _dry_live(s),
    "field_drainage": lambda s: s.q_downstream >= 0.75 * SAFE_CHANNEL,
    "lift_pumpset": lambda s: s.overtop_depth > 0.0,
    "harvest_ahead": lambda s: s.overtop_depth > 0.1,
    "move_livestock_high": lambda s: s.households_at_risk > 0,
    "warn_before_gate": lambda s: abs(s.firo_release - s.release_now) > 5.0,
    "gate_standby": _flood_live,
    "clear_spillway": _flood_live,
    "confirm_gauges": lambda s: s.q_downstream >= 0.75 * SAFE_CHANNEL,
    "second_operator": lambda s: abs(s.firo_release - s.release_now) > 5.0,
    "walk_channel": lambda s: s.q_downstream >= 0.75 * SAFE_CHANNEL,
    "photo_tailrace": lambda s: abs(s.firo_release - s.release_now) > 5.0,
    "log_surcharge": lambda s: s.flood_mode,
    "inspect_freeboard": lambda s: s.reservoir_pct >= 78.0 and _flood_live(s),
    "ration_release": _dry_bad,
    "audit_losses": _dry_live,
    "vulnerable_first": lambda s: s.households_at_risk > 0,
    "cut_power": lambda s: s.overtop_depth > 0.1,
    "back_marker": lambda s: s.households_at_risk > 0,
    "stage_boats": lambda s: s.q_downstream >= 0.75 * SAFE_CHANNEL,
    "close_road": lambda s: s.overtop_depth > 0.1,
    "shelter_provision": lambda s: s.households_at_risk > 0,
    "livestock_route": lambda s: s.households_at_risk > 0,
    "time_the_route": lambda s: not _flood_live(s),
    "water_tankers": _dry_bad,
    "fodder_depot": _dry_bad,
    "no_moving_water": lambda s: s.households_at_risk > 0,
    "documents_meds": lambda s: s.households_at_risk > 0,
    "boil_water": lambda s: s.overtop_depth > 0.1,
    "store_water": _dry_live,
}

# Urgency order for presentation. "now" first: a card that opens on a weekly
# measure during an evacuation has buried the thing that matters.
_HORIZON_RANK = {"now": 0, "next 6 hours": 1, "next 24 hours": 2,
                 "this week": 3}


def mitigations(s: BasinState, role: str | None = None,
                limit: int = 0) -> list:
    """The measures that apply to this state, most urgent first.

    ``role`` filters to one desk; a farmer card never shows gate practice.
    ``limit`` caps the list, because a card with eleven bullets is a card
    nobody reads. Returns the catalogue entries themselves, so a caller can
    only ever display a measure that exists in ``MITIGATIONS``.
    """
    out = []
    for m in MITIGATIONS:
        if role is not None and m.role != role:
            continue
        trigger = _MIT_TRIGGER.get(m.key)
        try:
            if trigger is None or not trigger(s):
                continue
        except Exception:                               # pragma: no cover
            continue
        out.append(m)
    out.sort(key=lambda m: _HORIZON_RANK.get(m.horizon, 9))
    return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# 3. WHERE TO GO — an order of movement, and criteria for the destination
#
# This console deliberately does NOT name a destination, and the reason is
# worth stating plainly because the alternative looks more impressive:
#
#   * The only candidate places in the data are the three zones a basin is
#     evacuated FROM, carrying elevation *labels* ("538 m") that are the demo
#     basin's figures reused verbatim for every region and never parsed as
#     numbers anywhere.
#   * The highest of those three is still a zone the app's own time-to-impact
#     banding paints critical once the levee goes, because it carries a finite
#     arrival delay. It is not a refuge.
#   * ``shelter_phrase`` is a sentence template over a region name. In live mode
#     it asserts a designated district facility this software has never checked.
#
# So naming a place would mean inventing one. What CAN be computed honestly is
# the order in which zones must move — time to impact plus each zone's own
# delay, which is real arithmetic over real engine output — together with the
# criteria a destination has to meet, and a handoff to the authority that holds
# the shelter register. In the scripted demo the curated basin may name its own
# shelter, because there the geography is part of the script.
# ---------------------------------------------------------------------------
@dataclass
class EvacStep:
    """One zone's place in the order of movement."""
    order: int          # 1 = moves first
    name: str
    zone: str
    households: int
    minutes: float      # time to impact for this zone; inf = none computed
    severity: str       # from the engine's own time-to-impact banding


# What a destination has to satisfy. Criteria, not coordinates — every one of
# these is checkable by a person standing in the place, which is the point.
DESTINATION_CRITERIA = (
    "Uphill and away from the river, not merely further along the bank.",
    "A building your district plan already designates — a school, community "
    "hall or municipal building — not a structure chosen on the day.",
    "Reachable without crossing the floodplain or any water already moving.",
    "Able to take livestock and vehicles as well as people, or paired with "
    "somewhere that can.",
)

DESTINATION_HANDOFF = (
    "Confirm the specific site against your district disaster-management plan. "
    "This console holds the basin's hydrology, not its shelter register."
)


def evacuation_order(s: BasinState, place: "Place | None" = None) -> list:
    """The zones in the order they must move. Computed, not advised.

    Each zone's time to impact is the basin's own ``time_to_overtop_min`` plus
    the delay the zone list carries for it, which is the same arithmetic the
    disaster tab's zone table already does — so this cannot disagree with it.
    """
    if place is None:
        place = DEMO_PLACE
    tto = s.time_to_overtop_min
    rows = []
    for z in place.sectors:
        if z.get("households") is not None:
            hh = int(z["households"])
        else:
            hh = int(round(s.households_at_risk * float(z.get("share", 0.0))))
        mins = (tto + float(z.get("delay", 0.0))
                if math.isfinite(tto) else float("inf"))
        rows.append({"name": str(z.get("name", "")), "zone": str(z.get("zone", "")),
                     "hh": hh, "mins": mins})
    rows.sort(key=lambda r: (r["mins"], -r["hh"]))
    out = []
    for i, r in enumerate(rows, start=1):
        if not math.isfinite(r["mins"]):
            sev = "safe"
        elif r["mins"] <= 100:
            sev = "critical"
        elif r["mins"] <= 120:
            sev = "warning"
        else:
            sev = "watch"
        out.append(EvacStep(i, r["name"], r["zone"], r["hh"], r["mins"], sev))
    return out


def safe_guidance(s: BasinState, place: "Place | None" = None,
                  scripted: bool = False) -> dict:
    """Where to go: the order, the criteria, and who owns the shelter register.

    ``scripted`` is True only for the curated demo basin, whose geography is
    part of the script and may therefore be named. In live mode the named site
    is withheld — see this section's header for why that is the honest choice
    rather than a missing feature.
    """
    if place is None:
        place = DEMO_PLACE
    steps = evacuation_order(s, place)
    moving = [st for st in steps if math.isfinite(st.minutes) and st.households > 0]
    return {
        "lead_minutes": s.time_to_overtop_min,
        "households": int(s.households_at_risk or 0),
        "steps": steps,
        "moving": moving,
        "criteria": list(DESTINATION_CRITERIA),
        "handoff": DESTINATION_HANDOFF,
        "named": place.shelter_phrase if scripted else None,
        "active": bool(moving),
    }
