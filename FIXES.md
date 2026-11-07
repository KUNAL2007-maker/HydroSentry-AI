# Audit fixes — HydroSentry-AI

All fixes are wording/labels, one logic fix (P0-1), and an ordinal helper.
No engine numbers, thresholds, or `simulate()` behaviour changed. The
original 5-tab frontend is kept — the 3-tab simplification was NOT re-applied.

## P0 — must fix

### P0-1 Feed failure after first load showed invented numbers as live
- `app.py` `render_dashboard()` (~line 2904): `elif first_load:` → `elif not (obs is not None and obs.ok):`
- Before: after one good reading, a failed feed fell through to
  `forcing_from_live()`, which invents neutral values (rain 0.6, soil 0.28,
  ET₀ 4.0, temp 30.0) and rendered them as if live.
- After: any failed live reading shows the `_render_live_unreachable` panel
  and no numbers. Stale-but-ok readings (`obs.ok` True, `stale` True) still
  render, with their cached label.

### P0-2 "Sent to 1,240 farmers" was never sent
- `app.py` Farmer tab (~lines 1414, 1421): heading "Message sent to farmers"
  → "Message for farmers"; card "Sent to 1,240 farmers in Marathi and Hindi."
  → "Draft SMS — not sent yet · 1,240 farmers on the list."
- The engine `sms["sent"] = 1240` dict was deliberately NOT changed, so the
  frozen demo snapshot is untouched by this fix.

## P1

### P1-1 KGE / POD / R / lead time are fixed constants, shown as measured
- `app.py` Overview metrics (~lines 1360–1363): the four cards are now titled
  "… — historical, fixed" with subtext "Fixed validation score/figure, not
  measured this update". Numbers unchanged (KGE 0.93, POD 0.57, R 0.89,
  lead 10 days).

### P1-2 Households / shelters are Pune demo constants for every region
- `app.py` Disaster tab (~line 1787): added a note under the readouts:
  "1,030 households is the mapped low-lying zone for the Pune demo basin —
  not a live census for other regions. Shelter and zone names are indicative
  — check the district plan." No per-region numbers invented.

### P1-3 Stale readings labelled "Live" with no age
- `app.py` `render_header()` live branch (~lines 1253–1265): when `obs.stale`
  is true, the header now reads "Live · updated HH:MM:SS · cached, X min old"
  (age from `obs.fetched_at`; falls back to "cached reading"). Sidebar's
  existing cached badge is unchanged.

### P1-4 "Live simulation" / "Time (IST)"
- `app.py` sidebar (~line 1060): checkbox "Live simulation" → "▶ Play
  simulation"; status pill "Live simulation" → "Playing simulation".
  It is scripted playback, not live data.
- `app.py` gate schedule (~line 1573): header "Time (IST)" →
  "Time (Asia/Kolkata)" — actually the basin's timezone from
  `ss.region_tz`, falling back to Asia/Kolkata.

### P1-5 Ordinal typos in the engine ("53th percentile")
- `hydro_engine.py` (~lines 741–750): added `ordinal_suffix()` /
  `esp_ordinal()` helpers (1st, 2nd, 3rd, 11th, 53rd, 93rd).
- Used in `make_directives()` (lines ~784, ~801) and `detect_patterns()`
  (lines ~1204, ~1216): "…th percentile" → correct ordinal.
- `app.py` already had the same fix on screen (Overview, gauge, map detail,
  lines ~594, ~1327, ~1420) via local `_ordinal_suffix()` / `_esp_ordinal()`.
- `_demo_golden.json` was regenerated with the same dump function as
  `verify_demo_golden.py` (`json.dumps(make_directives(simulate(scen, tick)),
  sort_keys=True, ensure_ascii=False)`, same single-line file format).
  Golden diff check: 76 of 132 states changed, and the ONLY differences are
  ordinal suffixes — 33th→33rd, 41th→41st, 93th→93rd. No numeric or state
  changes. (Remaining "…th percentile" strings, e.g. 89th, 50th, are correct.)

## P2

### P2-1 Mass balance "+0e+00 acre-ft"
- `app.py` Overview (~line 1364): residual exactly 0 now shows "0 acre-ft";
  non-zero values keep the existing `+0e` format.

### P2-2 Disaster "Time to impact 0 min"
- `app.py` Disaster tab (~line 1783): `tto <= 1` now shows "now" (matching
  the zone table, which already did this); 1–90 min still shows minutes.

### P2-3 Severity naming
- Chose "Elevated" everywhere, to match `SEV_LABEL` ("warning" → "Elevated").
- `app.py` basin-map legend (~line 611): "Warning" → "Elevated". The
  Disaster legend's "Stand by / Evacuate now" are time-based bands, not
  severity names, so they were left alone. No verify test asserts either
  label (checked).

### P2-4 Jargon glosses
- `app.py` Overview, under "Performance at a glance" (~line 1353): one line —
  "ESP = how dry the air is vs. the soil, as a percentile. KGE = how well the
  model matches past river data. POD = how often it catches a real drought.
  FIRO = releasing a little water early, before a flood, to make room for it."

### P2-5 Demo clock is a fixed past date
- `app.py` demo header (~line 1296): now reads "Demo — scripted 21 Sep 2026 ·
  Scenario: …", so it cannot be read as today.

### P2-6 "Live overview" label in Demo mode
- `app.py` Overview (~line 1377): "Basin snapshot" subtitle is "Live overview"
  in live mode and "Demo snapshot" in demo mode.

## Deliberately NOT changed
- Engine numbers, thresholds, `simulate()` behaviour: untouched.
- `sms["sent"] = 1240` in the engine: untouched (label fixed in the UI only).
- The 3-tab simplification: NOT re-applied; original 5 tabs kept.
- No per-region household/shelter numbers invented; no SMS provider added.
- No new dependencies, no new env vars.

## Tests (run after all fixes)
- `python -m py_compile app.py hydro_engine.py live_data.py nugen_client.py` — PASS
- `python verify_demo_golden.py` — 132/132 states identical (0 mismatches)
- `python verify_fixes.py` — 50/50 checks passed (0 failed)
- `python verify_guard.py` — PASS (0 mitigation actions wrongly flagged)
- `python verify_analyst.py` — 244/244 checks passed (0 failed)
- `python verify_app.py` (Streamlit AppTest) — 81/86, 5 failed (F7a, F9, J2, J3,
  J4). Pre-existing: the same suite on untouched HEAD 86b60f9 gives the
  identical 81/86 with the same 5 failures. Zero new failures.
- Smoke test: `streamlit run app.py --server.port 8510 --server.headless true`
  — `curl localhost:8510/` returned 200; server killed cleanly afterwards.

## Reservoir slider reset + mobile controls (7 Nov, after the audit push)

### Reservoir slider reset to 0% on Live -> Demo -> Live
- Root cause: the dam slider lived only in the Live branch of the sidebar, so
  switching to Demo destroyed the widget and its value reset to 0% on return.
- Fix: the slider now lives in a "Controls — Live / Demo & dam level" box at
  the top of the MAIN panel, rendered in BOTH modes, so its value survives
  the switch. In Demo mode it does not change the scripted scenario; it is
  kept so the Live value is not lost.
- Renamed in plain words for the operator: heading "Dam water level",
  slider "How full is the dam? (%)", button "Set to usual level (78%)",
  with the caption "How full is the dam right now? Move the slider to match
  the dam's actual filling — 78% is the usual level."
- Verified with Streamlit AppTest: slider starts 78, set to 45, switch to
  Demo (still 45), switch back to Live (still 45), zero exceptions.

### Sidebar not visible on mobile
- Root cause: on a phone Streamlit hides the sidebar behind the ">" menu
  button, so the Live/Demo switch and the dam slider were invisible.
- Fix: the Live/Demo switch and the dam slider moved out of the sidebar into
  the main-panel "Controls" box (above), which is always visible on mobile.
  The sidebar now points to it. The phone menu button was also made bigger
  (44px, bordered) so the rest of the sidebar is findable.
