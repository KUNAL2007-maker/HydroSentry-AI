"""
live_data.py — real-time basin observations for HydroSentry-AI
==============================================================

Fetches LIVE weather + hydrology for the Upper Bhima Basin (Pune, Maharashtra)
and returns it as a provider-neutral ``LiveObs`` snapshot. The dashboard's
Live mode feeds this snapshot into the SAME physics engine that drives the
demo (see ``hydro_engine.forcing_from_live``), so nothing about the UI or the
physics has to change to run on real data.

Design
------
* **Keyless by default.** The default provider is Open-Meteo — a free public
  API that needs no key, speaks HTTPS (so it works on Render and inside an
  iframe), and returns real rainfall, temperature, root-zone soil moisture and
  FAO evapotranspiration for any lat/lon, with hourly + daily forecasts.
* **Pluggable.** A different source (IMD, a college endpoint, a paid weather
  API…) can be dropped in later by implementing ``DataProvider.fetch`` and
  registering it in ``PROVIDERS``. Select one at runtime with the
  ``HYDRO_DATA_PROVIDER`` environment variable (default ``"open-meteo"``); a
  keyed source can read ``HYDRO_API_KEY`` / ``HYDRO_API_URL``.
* **Never crashes.** ``fetch_live`` wraps everything in try/except and returns
  ``LiveObs(ok=False, error=…)`` on any failure, so the app degrades to a clean
  "live data unavailable" state instead of throwing.

All numbers here are genuine observations/forecasts — no simulation.
"""

from __future__ import annotations

import copy
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

try:
    import requests
    HAS_REQUESTS = True
except Exception:                       # pragma: no cover - requests ships with streamlit
    HAS_REQUESTS = False


# ----------------------------------------------------------------------------
# Network tuning
# ----------------------------------------------------------------------------
# A local dev box reaches Open-Meteo in ~1-2 s, but a hosted instance (e.g.
# Render, US) hitting Open-Meteo (EU) over a possibly-cold connection can take
# longer — a tight 6 s timeout there shows a false "data unavailable". So use a
# generous default timeout and one retry with a growing budget. Both are
# overridable from the environment for stricter/looser hosting.
HTTP_TIMEOUT = float(os.environ.get("HYDRO_HTTP_TIMEOUT", "") or 12.0)
HTTP_RETRIES = int(os.environ.get("HYDRO_HTTP_RETRIES", "") or 1)
HTTP_HEADERS = {"User-Agent": "HydroSentry-AI/1.0 (+https://hydrosentry-ai.onrender.com)"}

# Last-good snapshot cache (per basin). Open-Meteo rate-limits by *IP*, and a
# free hosted tier (Render) shares one outbound IP across many apps, so a fetch
# can come back "429 Too Many Requests" even though THIS app calls it rarely.
# When that happens we serve the most recent successful reading (marked
# ``stale``) so the dashboard keeps running on REAL data instead of dropping to
# fallback. A snapshot is trusted for LAST_GOOD_TTL seconds (default 1 h).
LAST_GOOD_TTL = float(os.environ.get("HYDRO_LAST_GOOD_TTL", "") or 3600.0)


# ============================================================================
# Basin definition
# ============================================================================
@dataclass
class Basin:
    name: str
    lat: float
    lon: float
    tz: str = "Asia/Kolkata"


# The monitored basin. Pune sits at the outlet of the Upper Bhima catchment;
# this point is representative for basin-average rainfall, temperature, soil
# moisture and evapotranspiration.
UPPER_BHIMA = Basin("Upper Bhima Basin — Pune", 18.5204, 73.8567, "Asia/Kolkata")


# A few ready-made basins / cities so a presenter can switch region with one
# click, without typing. The first entry is the app's default basin.
PRESETS: list[Basin] = [
    UPPER_BHIMA,
    Basin("Nashik — Upper Godavari", 19.9975, 73.7898, "Asia/Kolkata"),
    Basin("Nagpur — Wainganga Basin", 21.1458, 79.0882, "Asia/Kolkata"),
    Basin("Mumbai — coastal", 19.0760, 72.8777, "Asia/Kolkata"),
    Basin("Kolhapur — Panchganga", 16.7050, 74.2433, "Asia/Kolkata"),
    Basin("Hyderabad — Musi Basin", 17.3850, 78.4867, "Asia/Kolkata"),
    Basin("Bengaluru — Arkavathy", 12.9716, 77.5946, "Asia/Kolkata"),
    Basin("Delhi — Yamuna Basin", 28.6139, 77.2090, "Asia/Kolkata"),
]


# ============================================================================
# Geocoding — turn a free-text place name into Basin(s)
# ============================================================================
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


def _place_label(r: dict) -> str:
    """Human label from an Open-Meteo geocoding result: 'Nashik, Maharashtra, India'."""
    parts = [r.get("name")]
    admin1 = r.get("admin1")
    country = r.get("country")
    if admin1 and admin1 != r.get("name"):
        parts.append(admin1)
    if country:
        parts.append(country)
    return ", ".join(p for p in parts if p)


def search_places(query: str, count: int = 6, timeout: float = 6.0) -> list[Basin]:
    """Search any place by name (Open-Meteo geocoding, keyless).

    Returns a list of ``Basin`` matches, best first. Never raises — on an
    empty query, missing ``requests``, or any network/API failure it returns
    ``[]`` so the caller can show 'no matches' cleanly.
    """
    query = (query or "").strip()
    if not query or not HAS_REQUESTS:
        return []
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"name": query, "count": count, "language": "en", "format": "json"},
            timeout=timeout,
            headers=HTTP_HEADERS,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except Exception:
        return []

    out: list[Basin] = []
    for r in results:
        lat, lon = _f(r.get("latitude")), _f(r.get("longitude"))
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        out.append(Basin(_place_label(r), lat, lon,
                         r.get("timezone") or "Asia/Kolkata"))
    return out


# ============================================================================
# Snapshot container (provider-neutral)
# ============================================================================
@dataclass
class LiveObs:
    ok: bool = False
    source: str = "—"
    fetched_at: datetime | None = None
    error: str | None = None
    stale: bool = False                  # served from last-good cache (see fetch_live)

    # --- real current readings ------------------------------------------
    temp_now: float = float("nan")       # °C, 2 m air temperature
    precip_now: float = 0.0              # mm in the current hour
    humidity: float = float("nan")       # %, relative humidity
    soil_moisture: float = float("nan")  # m³/m³, root-zone (9–27 cm)
    et0_now: float = float("nan")        # mm/day, FAO reference ET

    # --- derived forcing / forecast -------------------------------------
    rain_peak: float = 0.0               # mm/hr, peak over the flood horizon
    rain_peak_in_h: float = 0.0          # hours from now to that peak
    precip_next24: float = 0.0           # mm, total over the next 24 h
    tmax_fc: float = float("nan")        # °C, forecast daily max
    heat: float = 0.0                    # °C above the 30 °C drought baseline
    dry: bool = False                    # little rain coming -> dry spell

    # --- real recent soil-moisture trend (for the drought chart) --------
    sm_days: np.ndarray = field(default_factory=lambda: np.array([]))
    sm_series: np.ndarray = field(default_factory=lambda: np.array([]))


# ============================================================================
# Provider interface + implementations
# ============================================================================
FLOOD_HORIZON_H = 8          # look-ahead window for the rainfall peak
DROUGHT_BASELINE_C = 30.0    # temperature baseline (matches hydro_engine)
DRY_THRESHOLD_MM = 2.0       # <2 mm over the next 24 h == effectively dry


class DataProvider:
    """Base class. Implement ``fetch`` and register in ``PROVIDERS``."""

    name = "base"

    def fetch(self, basin: Basin, timeout: float = 6.0) -> LiveObs:
        raise NotImplementedError


def _to_dt(s: str) -> datetime:
    """Parse an Open-Meteo ISO timestamp like '2026-09-25T14:00'."""
    return datetime.fromisoformat(s)


def _nearest_index(times: list[str], now: datetime) -> int:
    """Index of the hourly sample closest to 'now'."""
    best_i, best_d = 0, None
    for i, t in enumerate(times):
        try:
            d = abs((_to_dt(t) - now).total_seconds())
        except Exception:
            continue
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    return best_i


class OpenMeteoProvider(DataProvider):
    """Free, keyless global weather + hydrology (https://open-meteo.com)."""

    name = "Open-Meteo (live)"
    URL = "https://api.open-meteo.com/v1/forecast"

    def fetch(self, basin: Basin, timeout: float = 6.0) -> LiveObs:
        if not HAS_REQUESTS:
            raise RuntimeError("the 'requests' package is not available")
        params = {
            "latitude": basin.lat,
            "longitude": basin.lon,
            "current": ("temperature_2m,relative_humidity_2m,precipitation,rain,"
                        "soil_moisture_0_to_1cm"),
            "hourly": ("precipitation,temperature_2m,soil_moisture_9_to_27cm,"
                       "et0_fao_evapotranspiration"),
            "daily": "temperature_2m_max,precipitation_sum,et0_fao_evapotranspiration",
            "past_days": 2,
            "forecast_days": 3,
            "timezone": basin.tz,
        }
        r = requests.get(self.URL, params=params, timeout=timeout, headers=HTTP_HEADERS)
        r.raise_for_status()
        return self._map(r.json())

    def _map(self, j: dict) -> LiveObs:
        o = LiveObs(source=self.name)
        cur = j.get("current", {}) or {}
        hourly = j.get("hourly", {}) or {}
        daily = j.get("daily", {}) or {}

        times = hourly.get("time", []) or []
        precip = hourly.get("precipitation", []) or []
        temp_h = hourly.get("temperature_2m", []) or []
        soil_h = hourly.get("soil_moisture_9_to_27cm", []) or []

        # current readings
        o.temp_now = _f(cur.get("temperature_2m"))
        o.precip_now = _f(cur.get("precipitation"), 0.0)
        o.humidity = _f(cur.get("relative_humidity_2m"))

        # locate "now" within the hourly arrays
        now = _to_dt(cur["time"]) if cur.get("time") else datetime.now()
        i = _nearest_index(times, now) if times else 0

        # root-zone soil moisture (real), with a shallow-layer fallback
        o.soil_moisture = _at(soil_h, i)
        if not math.isfinite(o.soil_moisture):
            o.soil_moisture = _f(cur.get("soil_moisture_0_to_1cm"))

        # rainfall: peak intensity over the next FLOOD_HORIZON_H hours
        fut = precip[i:i + FLOOD_HORIZON_H + 1]
        if fut:
            k = int(np.nanargmax(fut))
            o.rain_peak = float(fut[k])
            o.rain_peak_in_h = float(k)
        o.precip_next24 = float(np.nansum(precip[i:i + 24])) if precip else 0.0

        # forecast daily max temperature (drives heat); prefer daily, else hourly
        dmax = daily.get("temperature_2m_max", []) or []
        o.tmax_fc = _at(dmax, _today_index(daily, now))
        if not math.isfinite(o.tmax_fc) and temp_h[i:i + 24]:
            o.tmax_fc = float(np.nanmax(temp_h[i:i + 24]))
        o.heat = max(0.0, o.tmax_fc - DROUGHT_BASELINE_C) if math.isfinite(o.tmax_fc) else 0.0
        o.dry = o.precip_next24 < DRY_THRESHOLD_MM

        # reference evapotranspiration today (mm/day) — real atmospheric demand
        det = daily.get("et0_fao_evapotranspiration", []) or []
        o.et0_now = _at(det, _today_index(daily, now))
        et_h = hourly.get("et0_fao_evapotranspiration", []) or []
        if not math.isfinite(o.et0_now) and et_h[i:i + 24]:
            o.et0_now = float(np.nansum(et_h[i:i + 24]))

        # real soil-moisture trend over the past ~48 h -> drought chart
        lo = max(0, i - 48)
        xs, ys = [], []
        for k in range(lo, i + 1):
            v = _at(soil_h, k)
            if math.isfinite(v):
                xs.append((k - lo) / 24.0)     # increasing "days" axis
                ys.append(v)
        if len(ys) >= 2:
            o.sm_days = np.array(xs)
            o.sm_series = np.array(ys)
        elif math.isfinite(o.soil_moisture):
            o.sm_days = np.array([0.0])
            o.sm_series = np.array([o.soil_moisture])
        return o


class CustomProvider(DataProvider):
    """Stub for a keyed / official source — wire your own feed in here.

    Read credentials from the environment and map the response into a
    ``LiveObs`` exactly like ``OpenMeteoProvider._map`` does, then select this
    provider at runtime with ``HYDRO_DATA_PROVIDER=custom``.
    """

    name = "Custom (keyed)"

    def fetch(self, basin: Basin, timeout: float = 6.0) -> LiveObs:
        api_key = os.environ.get("HYDRO_API_KEY")
        base_url = os.environ.get("HYDRO_API_URL")
        # TODO: call your source with (base_url, api_key, basin.lat, basin.lon),
        #       then build and return a LiveObs. Until then this stays disabled
        #       and the app falls back to Open-Meteo / the offline state.
        raise NotImplementedError(
            "Custom data provider is not configured. Set HYDRO_API_URL / "
            "HYDRO_API_KEY and implement CustomProvider.fetch in live_data.py."
        )


# registry — add your provider here
PROVIDERS: dict[str, DataProvider] = {
    "open-meteo": OpenMeteoProvider(),
    "custom": CustomProvider(),
}


def active_provider() -> DataProvider:
    """Provider chosen by HYDRO_DATA_PROVIDER (default: open-meteo)."""
    name = os.environ.get("HYDRO_DATA_PROVIDER", "open-meteo").strip().lower()
    return PROVIDERS.get(name, PROVIDERS["open-meteo"])


# ============================================================================
# Public entry point
# ============================================================================
# Per-basin cache of the last SUCCESSFUL snapshot: {basin_key: (monotonic_ts, obs)}.
# Module-level, so it survives Streamlit's per-interaction reruns (the module is
# imported once per process, unlike the script, which re-executes every rerun).
_LAST_GOOD: dict[tuple[float, float], tuple[float, LiveObs]] = {}


def _basin_key(b: Basin) -> tuple[float, float]:
    return (round(b.lat, 4), round(b.lon, 4))


def _remember(basin: Basin, obs: LiveObs) -> None:
    _LAST_GOOD[_basin_key(basin)] = (time.monotonic(), obs)


def _recall(basin: Basin) -> LiveObs | None:
    """Most recent good snapshot for this basin, if still within LAST_GOOD_TTL."""
    item = _LAST_GOOD.get(_basin_key(basin))
    if not item:
        return None
    ts, obs = item
    if time.monotonic() - ts > LAST_GOOD_TTL:
        return None
    return obs


def _is_client_error(e: Exception) -> bool:
    """True for an HTTP 4xx (esp. 429). Retrying these is pointless and, for a
    rate-limit, actively harmful — it just adds another call to the same IP."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500


def fetch_live(basin: Basin = UPPER_BHIMA, timeout: float | None = None) -> LiveObs:
    """Fetch a live snapshot. Always returns a LiveObs — never raises.

    Resilience, in order:

    1. Try the provider. A timeout / connection error is retried once with a
       larger budget (a hosted instance's first outbound call can be slow).
    2. An HTTP **4xx is NOT retried** — a 429 "Too Many Requests" (common on a
       shared hosting IP against Open-Meteo's per-IP rate limit) would only get
       worse with another call.
    3. If every attempt fails but we hold a recent good reading for this basin,
       serve it marked ``stale=True`` (``ok`` stays True) so the forecast keeps
       running on REAL data rather than dropping to fallback.
    4. Only with no usable cached reading do we return ``ok=False`` (with the
       last error) so the UI can explain what happened.
    """
    prov = active_provider()
    budget = HTTP_TIMEOUT if timeout is None else timeout
    last_err: Exception | None = None
    for _ in range(max(1, HTTP_RETRIES + 1)):
        try:
            obs = prov.fetch(basin, timeout=budget)
            obs.ok = True
            obs.stale = False
            obs.source = prov.name
            obs.fetched_at = datetime.now()
            _remember(basin, obs)
            return obs
        except Exception as e:                      # network down, API change, offline…
            last_err = e
            if _is_client_error(e):                 # 429 / other 4xx — don't hammer it
                break
            budget = min(budget * 1.6, 30.0)        # give the next attempt more room

    cached = _recall(basin)
    if cached is not None:
        stale = copy.copy(cached)                   # keep the original fetched_at / readings
        stale.stale = True
        stale.error = str(last_err)[:200] if last_err else None
        return stale
    return LiveObs(ok=False, source=prov.name,
                   fetched_at=datetime.now(), error=str(last_err)[:200])


# ============================================================================
# tiny numeric helpers
# ============================================================================
def _f(v, default=float("nan")) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _at(arr, i, default=float("nan")) -> float:
    try:
        return float(arr[i])
    except (TypeError, ValueError, IndexError):
        return default


def _today_index(daily: dict, now: datetime) -> int:
    """Index in the daily arrays whose date matches 'now' (else 0)."""
    dates = daily.get("time", []) or []
    today = now.date().isoformat()
    for i, d in enumerate(dates):
        if str(d).startswith(today):
            return i
    return 0
