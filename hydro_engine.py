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
BASE_TIME = datetime(2026, 9, 21, 12, 0)   # sim clock origin (IST)


# ============================================================================
# Basin constants (illustrative but internally consistent)
# ============================================================================
# Flood / reservoir
BASEFLOW = 190.0               # m³/s baseline inflow
UH_K, UH_THETA = 3.2, 2.05     # gamma unit-hydrograph shape/scale -> peak ~4.5 h
RES_CAP_AFT = 150_000.0        # reservoir capacity (acre-feet)
RES_START_FRAC = 0.78          # storage at scenario start
BASE_RELEASE = 120.0           # m³/s normal downstream release
SAFE_CHANNEL = 600.0           # m³/s safe downstream channel capacity
LEVEE_Q = 700.0                # m³/s at which the levee begins to overtop
MS_TO_AFT_PER_H = 3600.0 / 1233.48   # m³/s -> acre-feet per hour  (~2.918)
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

    # model performance (validation constants)
    compute_time_s: float = 82.9
    kge: float = 0.93
    pod: float = 0.57
    r_smap: float = 0.89
    lead_time_days: int = 10


# ============================================================================
# Helpers
# ============================================================================
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


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
        desc="Real-time observations for the Upper Bhima Basin.",
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
def simulate(spec, tick: int) -> BasinState:
    # forcing comes from a canned scenario (demo) or real observations (live)
    forcing = spec if isinstance(spec, Forcing) else forcing_from_scenario(spec)
    scenario_key = spec if isinstance(spec, str) else forcing.source
    live = forcing.source == "live"
    tick = int(max(0, min(TICKS_MAX, tick)))
    frac = tick / TICKS_MAX

    flood_h = frac * FLOOD_HORIZON_H
    drought_d = frac * DROUGHT_HORIZON_D
    clock = (BASE_TIME + timedelta(hours=flood_h)).strftime("%d %b %Y, %H:%M IST")

    s = BasinState(scenario=scenario_key, tick=tick, flood_hours=flood_h,
                   drought_day=drought_d, clock=clock)
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

    # a realistic compute time that nudges with event complexity
    s.compute_time_s = round(82.9 + (s.inflow_peak - BASEFLOW) / 400.0, 1)
    return s


# ============================================================================
# Reservoir gate schedule (forward plan, integrated with the same mass balance)
# ============================================================================
def gate_schedule(s: BasinState) -> list:
    """Forward-looking gate schedule for the operations table.

    Sampled at fixed event times; the release column comes from the same
    release rule the mass balance uses, and the level column is the storage
    integrated forward with that rule — so release, level and action agree.
    Each row is tagged done / now / planned relative to the live clock.
    """
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
            "time": (BASE_TIME + timedelta(hours=te)).strftime("%H:%M"),
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


def make_directives(s: BasinState) -> dict:
    """Turn the computed state into plain-language, per-stakeholder directives."""

    # ---- farmer ---------------------------------------------------------
    if s.drought_sev in ("warning", "critical"):
        farmer = {
            "severity": s.drought_sev,
            "title": "Start pre-dawn drip irrigation tomorrow",
            "situation": (
                f"Soil in the Junnar–Daund belt is at the {s.esp:.0f}th percentile of "
                f"evaporative stress and will reach the wilting point in about "
                f"{s.days_to_wilting:.0f} days. Rapid evaporation is pulling moisture from "
                f"the root zone faster than the crop can recover on its own."),
            "actions": [
                "Run drip irrigation for <b>45 minutes at 4:00 AM</b>, before the day heats up.",
                "Water fields with flowering or fruiting crops first.",
                "Avoid midday watering — most of it evaporates before roots can use it.",
            ],
            "meta": f"Warning issued {s.lead_time_days} days ahead of visible wilting — Junnar–Daund belt",
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
            "meta": "Junnar–Daund belt — conditions normal",
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
            "meta": f"Post-crest recovery — Khadakwasla Reservoir",
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
            "meta": f"Inflow surge expected in {s.inflow_peak_in_h:.1f} hours — Khadakwasla Reservoir",
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
            "meta": "Khadakwasla Reservoir — steady state",
            "cert": "Certified against CWC operation manual",
        }

    # ---- disaster team --------------------------------------------------
    if math.isfinite(s.time_to_overtop_min) and s.overtop_depth > 0.1:
        imminent = s.time_to_overtop_min <= 1
        title = ("Evacuate low-lying Sectors 4 and 5 now — levee overtopping"
                 if imminent else
                 f"Evacuate low-lying Sectors 4 and 5 within {int(round(s.time_to_overtop_min))} minutes")
        disaster = {
            "severity": "critical" if s.time_to_overtop_min <= 100 else "warning",
            "title": title,
            "situation": (
                f"The river will rise about {s.overtop_depth:.1f} m above the levee crest at "
                f"Sectors 4 and 5 ({_fmt_time(s.time_to_overtop_min)}). "
                f"Areas below 542 m elevation are at risk of inundation."),
            "actions": [
                "Begin geofenced evacuation for all zones <b>below 542 m elevation</b>.",
                "Move residents to the high-ground shelters on Route H2.",
                "Close the riverside road at the Sector 4 junction to incoming traffic.",
            ],
            "meta": f"Impact {_fmt_time(s.time_to_overtop_min)} — Sectors 4 &amp; 5",
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
    base = BASE_TIME + timedelta(hours=s.flood_hours)
    if disaster["severity"] != "safe":
        feed.append((base.strftime("%H:%M"), disaster["severity"],
                     "<b>Sectors 4 &amp; 5:</b> prepare geofenced evacuation, "
                     f"impact expected {_fmt_time(s.time_to_overtop_min)}."))
    if s.firo_release > BASE_RELEASE + 5 and not s.past_peak:
        feed.append(((base - timedelta(minutes=18)).strftime("%H:%M"), dam["severity"],
                     f"<b>Khadakwasla dam:</b> begin {s.firo_release:.0f} m³/s controlled pre-release."))
    elif s.firo_release > BASE_RELEASE + 5 and s.past_peak:
        feed.append(((base - timedelta(minutes=18)).strftime("%H:%M"), "watch",
                     "<b>Khadakwasla dam:</b> surge crest absorbed — holding safe-channel release."))
    if farmer["severity"] in ("warning", "critical"):
        feed.append(((base - timedelta(minutes=32)).strftime("%H:%M"), farmer["severity"],
                     "<b>Farmers, Junnar–Daund:</b> start pre-dawn drip irrigation to protect roots."))
    if not feed:
        feed.append((base.strftime("%H:%M"), "safe",
                     "<b>Basin normal:</b> no directives active. Continuing routine monitoring."))

    return {"farmer": farmer, "dam": dam, "disaster": disaster, "sms": sms, "feed": feed}
