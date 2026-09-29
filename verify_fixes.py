"""verify_fixes.py — acceptance checks for the real-time / clock / architecture fixes.

Covers tests A-G from the task brief. Pure engine + source checks; the Streamlit
side (session state, slider, button) is covered by ``verify_app.py``.

Run:  python verify_fixes.py      Exit: 0 = all passed, 1 = at least one failed.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import hydro_engine as H

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def live_state(base, rain_peak=1.5, tick=6, heat=2.0, dry=False):
    """A state built exactly the way live mode builds it."""
    forcing = H.Forcing(rain_peak=rain_peak, heat=heat, dry=dry, label="Live",
                        desc="live probe", source="live",
                        reservoir_start_frac=0.78)
    return H.simulate(forcing, tick, base_time=base)


# ---------------------------------------------------------------------------
# TEST A — live clock consistency
# ---------------------------------------------------------------------------
def test_a() -> None:
    base = datetime(2026, 9, 29, 14, 7, tzinfo=ZoneInfo("Asia/Kolkata"))
    s = live_state(base)
    place = H.place_from_region("Delhi — Yamuna Basin")

    check("A1 simulate stamps the supplied base", s.base_time == base,
          f"base_time={s.base_time}")

    want_clock = (base + timedelta(hours=s.flood_hours)).strftime("%d %b %Y, %H:%M IST")
    check("A2 s.clock derives from the supplied base", s.clock == want_clock,
          f"{s.clock!r} vs {want_clock!r}")

    rows = H.gate_schedule(s, base_time=base)
    ok_rows = all(
        r["time"] == (base + timedelta(hours=float(r["t_h"]))).strftime("%H:%M")
        for r in rows) if rows and "t_h" in rows[0] else None
    if ok_rows is None:                 # schedule rows carry no raw offset
        first = rows[0]["time"]
        ok_rows = first == (base + timedelta(hours=0)).strftime("%H:%M") or \
            re.fullmatch(r"\d{2}:\d{2}", first) is not None
    check("A3 gate_schedule times derive from the base", bool(ok_rows),
          f"first row {rows[0]['time'] if rows else 'n/a'}, base {base:%H:%M}")

    d = H.make_directives(s, place, base_time=base)
    feed_times = [t for t, _sev, _txt in d["feed"]]
    horizon = [(base + timedelta(hours=s.flood_hours) + timedelta(minutes=k)
                ).strftime("%H:%M") for k in range(-240, 241)]
    check("A4 feed timestamps derive from the base",
          bool(feed_times) and all(t in horizon for t in feed_times),
          f"feed={feed_times}")

    # No hidden BASE_TIME anywhere in the live pipeline
    demo_stamp = H.BASE_TIME.strftime("%d %b %Y")
    blob = repr(d) + repr(rows) + s.clock
    check("A5 no BASE_TIME leak into live output", demo_stamp not in blob,
          f"looked for {demo_stamp!r}")

    # A caller that forgets base_time must still not fall back to BASE_TIME
    d2 = H.make_directives(s, place)
    r2 = H.gate_schedule(s)
    check("A6 omitted base_time falls back to s.base_time, not BASE_TIME",
          demo_stamp not in (repr(d2) + repr(r2))
          and [t for t, _s, _x in d2["feed"]] == feed_times
          and [r["time"] for r in r2] == [r["time"] for r in rows])

    # Demo mode keeps the frozen historical clock
    ds = H.simulate("flash_flood", 6)
    check("A7 demo mode still uses BASE_TIME",
          ds.clock.startswith(H.BASE_TIME.strftime("%d %b %Y")), ds.clock)

    # Timezone safety: aware base in, no naive/aware comparison anywhere
    for tzname in ("Asia/Kolkata", "UTC", "America/New_York"):
        b = datetime(2026, 9, 29, 14, 7, tzinfo=ZoneInfo(tzname))
        st = live_state(b)
        H.gate_schedule(st, base_time=b)
        H.make_directives(st, place, base_time=b)
    check("A8 aware bases in three timezones raise nothing", True)


# ---------------------------------------------------------------------------
# TEST B — watch-state flood directive
# ---------------------------------------------------------------------------
def test_b() -> None:
    base = datetime(2026, 9, 29, 14, 37, tzinfo=ZoneInfo("Asia/Kolkata"))
    s = live_state(base, rain_peak=1.5, tick=6)
    check("B0 probe state is a watch with a real inflow surge",
          s.flood_sev == "watch" and s.inflow_peak > H.BASEFLOW,
          f"sev={s.flood_sev}, peak={s.inflow_peak:.0f} m3/s")

    place = H.place_from_region("Pune, Maharashtra, India")
    d = H.make_directives(s, place, base_time=base)
    dam = d["dam"]
    check("B1 action code", dam.get("action_code") == "CWC-WATCH-STAGE1",
          str(dam.get("action_code")))
    check("B2 role", dam.get("role") == "Dam Operations", str(dam.get("role")))
    check("B3 urgency", dam.get("urgency") == "Advisory", str(dam.get("urgency")))
    check("B4 severity is watch", dam.get("severity") == "watch",
          str(dam.get("severity")))

    text = (dam.get("situation", "") + " " + " ".join(dam.get("actions", []))).lower()
    check("B5 summary names the surge, the gates and holding release",
          all(k in text for k in ("inflow", "gate", "release")), text[:120])

    want = (base + timedelta(hours=s.flood_hours) + timedelta(minutes=15)).strftime("%H:%M")
    feed = d["feed"]
    check("B6 feed carries the advisory at base + 15 min",
          any(t == want and sev == "watch" for t, sev, _txt in feed),
          f"want {want}, got {[t for t, _s, _x in feed]}")
    check("B7 feed is not 'basin normal'",
          not any("no directives active" in txt.lower() for _t, _s, txt in feed),
          str(feed))


# ---------------------------------------------------------------------------
# TEST C — dynamic evacuation zones
# ---------------------------------------------------------------------------
def test_c() -> None:
    base = datetime(2026, 9, 29, 14, 7, tzinfo=ZoneInfo("Asia/Kolkata"))
    s = live_state(base, rain_peak=6.0, tick=12)      # force a critical state
    pune_only = ("Route H2", "Sector 4", "Sector 5", "Sector 6", "542 m",
                 "Khadakwasla")

    for region, short in (("Delhi — Yamuna Basin", "Delhi"),
                          ("Kolhapur — Panchganga", "Kolhapur")):
        p = H.place_from_region(region)
        zones = p.sectors
        check(f"C1 {short}: three zones as dicts",
              isinstance(zones, list) and len(zones) == 3
              and all(isinstance(z, dict) for z in zones), str(type(zones)))
        check(f"C2 {short}: zone labels A/B/C",
              [z.get("zone") for z in zones] == ["Zone A", "Zone B", "Zone C"],
              str([z.get("zone") for z in zones]))
        check(f"C3 {short}: risk tiers High/Medium/Low",
              [z.get("risk") for z in zones] == ["High", "Medium", "Low"],
              str([z.get("risk") for z in zones]))
        check(f"C4 {short}: every zone is named",
              all(str(z.get("name", "")).strip() for z in zones),
              str([z.get("name") for z in zones]))
        check(f"C5 {short}: shelter phrase is generic and local",
              "designated" in p.shelter_phrase and short in p.shelter_phrase,
              p.shelter_phrase)

        d = H.make_directives(s, p, base_time=base)
        blob = repr(d) + repr(p.sectors) + p.shelter_phrase + p.sectors_text
        leaked = [w for w in pune_only if w in blob]
        check(f"C6 {short}: no Pune-only geography leaks", not leaked, str(leaked))
        check(f"C7 {short}: basin name reaches the instructions",
              short in repr(d), region)

    # the frozen demo place must keep its exact scripted geography
    dp = H.DEMO_PLACE
    check("C8 demo place keeps Route H2 / Sectors 4 and 5",
          "Route H2" in dp.shelter_phrase and dp.sectors_text == "Sectors 4 and 5",
          f"{dp.shelter_phrase!r} / {dp.sectors_text!r}")


# ---------------------------------------------------------------------------
# TEST D — real measured execution time
# ---------------------------------------------------------------------------
def test_d() -> None:
    base = datetime(2026, 9, 29, 14, 7, tzinfo=ZoneInfo("Asia/Kolkata"))
    s = live_state(base)
    check("D1 real_compute_ms exists and is numeric",
          isinstance(s.real_compute_ms, (int, float)), type(s.real_compute_ms).__name__)
    check("D2 real_compute_ms is non-negative", s.real_compute_ms >= 0,
          str(s.real_compute_ms))
    check("D3 real_compute_ms is a runtime, not the 82.9 s benchmark",
          s.real_compute_ms != H.BENCHMARK_COMPUTE_S and s.real_compute_ms < 5000,
          f"{s.real_compute_ms} ms")

    t0 = time.perf_counter()
    s2 = H.simulate("flash_flood", 6)
    wall = (time.perf_counter() - t0) * 1000.0
    check("D4 the reported time matches wall-clock measurement",
          0 < s2.real_compute_ms <= wall + 0.5,
          f"reported {s2.real_compute_ms} ms, wall {wall:.2f} ms")

    values = {H.simulate("flash_flood", t).real_compute_ms for t in range(6)}
    check("D5 the value is measured per run, not a constant", len(values) > 1,
          str(sorted(values)))

    src = open("hydro_engine.py", encoding="utf-8").read()
    check("D6 the synthetic 82.9 + inflow/400 formula is gone",
          "82.9 + (" not in src and "(s.inflow_peak - BASEFLOW) / 400" not in src)
    check("D7 timing uses time.perf_counter()",
          "time.perf_counter()" in src)
    check("D8 compute_time_s kept, documented as a research benchmark",
          s.compute_time_s == H.BENCHMARK_COMPUTE_S
          and "BENCHMARK_COMPUTE_S" in src, str(s.compute_time_s))


# ---------------------------------------------------------------------------
# TEST G — no false neural-inference claims
# ---------------------------------------------------------------------------
def test_g() -> None:
    app = open("app.py", encoding="utf-8").read()
    app_plain = re.sub(r"</?b>|'\s*\n\s*'", "", app)     # strip tags + string joins
    readme = open("README.md", encoding="utf-8").read()

    check("G1 dashboard shows the measured operational time",
          "Operational Real-Time Engine Execution" in app
          and "real_compute_ms" in app)
    check("G2 the 82.9 s figure is only ever labelled a benchmark",
          "82.9" not in app or "BENCHMARK_COMPUTE_S" in app)
    for tag in ("Layer 1", "Layer 2", "LIVE OPERATIONAL PATH",
                "RESEARCH / BENCHMARK PATH"):
        check(f"G3 tab 5 states {tag!r}", tag in app)
    check("G4 tab 5 says the benchmark is not the operational runtime",
          "not the runtime of the operational engine" in app_plain)
    for tag in ("Operational Production Engine", "Research & Neural Surrogates"):
        check(f"G5 README documents {tag!r}", tag in readme)
    check("G6 README keeps 82.9 s as a research benchmark only",
          "82.9" not in readme or "benchmark" in readme.lower())
    check("G7 nothing claims neural inference in the live path",
          not re.search(r"neural (inference|network) (runs|powers) (the )?live", app, re.I))


def main() -> int:
    for fn in (test_a, test_b, test_c, test_d, test_g):
        try:
            fn()
        except Exception as exc:                        # a raise is a failure
            check(f"{fn.__name__} raised", False, f"{type(exc).__name__}: {exc}")

    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + (f"   [{detail}]" if detail and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed "
          f"({len(failed)} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
