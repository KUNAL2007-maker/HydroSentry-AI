"""verify_app.py — Streamlit-side acceptance checks (tests E and F).

``verify_fixes.py`` covers the engine and the source tree; this file drives the
real dashboard through Streamlit's ``AppTest`` harness, which executes ``app.py``
exactly as the server does and exposes the resulting widgets, session state and
exceptions.

Two things are checked here that cannot be checked any other way:

* **Test E** — a first-time visitor lands in *live* mode, and the demo is still
  one click away (and switching either way raises nothing).
* **Test F** — the manual reservoir slider works, is labelled as operator input,
  and the "Set to Seasonal Normal (78%)" button moves it without tripping
  Streamlit's "widget created with a default value but also set via Session
  State" error.
* **Test H** — the other half of live-by-default: when the feed cannot be
  reached, the dashboard is *replaced* by an honest panel with a retry, so a
  fallback state is never dressed up as real observations.
* **Test I** — the Nugen briefing layer is wired into all three role tabs, is
  off without a key, and stays inside its call budget: never automatic in the
  demo, and at most one call per role per situation when live.
* **Test J** — the cloud case: the server's own feed call is refused while the
  visitor's in-browser fetch is still in flight, so the first render must say
  "fetching", and a miss that outlives that grace must still say "unreachable".
* **Test K** — the AI analyst tab: a question is answered from the computed
  state with no key and no network, an out-of-scope question is declined rather
  than guessed, the harness reports every stage, and the closing speed
  comparison still admits which of its two figures was measured here.

The live feed is stubbed with a fixed observation so the run is offline and
deterministic; the browser-side JS fetch is disabled through the app's own
``HYDRO_BROWSER_LIVE`` switch for the same reason.

Run:  python verify_app.py      Exit: 0 = all passed, 1 = at least one failed.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime

# Must be set before app.py is executed: it disables the browser-side fetch
# component, which has no meaning outside a real browser session.
os.environ["HYDRO_BROWSER_LIVE"] = "0"

import numpy as np

import hydro_engine as H
import live_data
import nugen_client

try:
    from streamlit.testing.v1 import AppTest
except Exception as exc:                                    # pragma: no cover
    print(f"  SKIP  streamlit.testing.v1 unavailable: {exc}")
    sys.exit(0)

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
TIMEOUT = 90                     # first run compiles the app and builds charts

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


# ---------------------------------------------------------------------------
# Offline live feed
# ---------------------------------------------------------------------------
def stub_obs(basin: live_data.Basin = live_data.UPPER_BHIMA) -> live_data.LiveObs:
    """A good, fixed reading — enough rain to make the state interesting."""
    return live_data.LiveObs(
        ok=True, source="verify_app stub", fetched_at=datetime.now(),
        temp_now=31.4, precip_now=2.4, humidity=71.0,
        soil_moisture=0.213, et0_now=5.2,
        rain_peak=6.0, rain_peak_in_h=3.0, precip_next24=48.0,
        tmax_fc=33.6, heat=3.6, dry=False,
        sm_days=np.arange(-13.0, 1.0),
        sm_series=np.linspace(0.30, 0.213, 14),
    )


def install_stub() -> None:
    """Point every live-fetch entry point at the stub (app.py calls through)."""
    live_data.get_live_cached = lambda basin=live_data.UPPER_BHIMA: stub_obs(basin)
    live_data.fetch_live = lambda basin=live_data.UPPER_BHIMA: stub_obs(basin)


def dead_obs(basin: live_data.Basin = live_data.UPPER_BHIMA) -> live_data.LiveObs:
    """What ``live_data`` hands back when the network is unreachable."""
    return live_data.LiveObs(ok=False, source="verify_app stub", fetched_at=None,
                             error="verify_app: simulated network failure")


def install_dead_stub() -> None:
    live_data.get_live_cached = lambda basin=live_data.UPPER_BHIMA: dead_obs(basin)
    live_data.fetch_live = lambda basin=live_data.UPPER_BHIMA: dead_obs(basin)


def fresh_app() -> "AppTest":
    install_stub()
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    return at


def widget_keys(at) -> set[str]:
    """Every ``kind:key`` on the page — used to assert a widget is *absent*."""
    found: set[str] = set()
    for kind in ("button", "slider", "radio"):
        try:
            found |= {f"{kind}:{w.key}" for w in getattr(at, kind)}
        except Exception:
            pass
    return found


def exc_text(at) -> str:
    return " | ".join(str(e.value) for e in at.exception)


def page_text(at) -> str:
    """Every string the page rendered (markdown, captions, alerts)."""
    parts: list[str] = []
    for kind in ("markdown", "caption", "text", "info", "warning", "error",
                 "success"):
        try:
            parts += [str(el.value) for el in getattr(at, kind)]
        except Exception:
            pass
    return "\n".join(parts)


def widget(at, kind: str, key: str):
    """A widget by key, or None — AppTest raises KeyError when it is absent."""
    try:
        return getattr(at, kind)(key=key)
    except (KeyError, AttributeError):
        return None


_VALUE_RE = re.compile(r'"value":\s*(-?[0-9.]+)')


def gauge_values(at) -> set[str]:
    """Every numeric ``value`` in the page's Plotly figures.

    The reservoir reading is a Plotly gauge, so it is not in any text element.
    Streamlit 1.64's AppTest has no typed ``plotly_chart`` accessor, but the
    element still carries its figure spec, which is enough to read the number
    the operator actually sees.
    """
    out: set[str] = set()
    for el in at.main:
        if type(el).__name__ != "UnknownElement" or el.type != "plotly_chart":
            continue
        spec = getattr(el.proto, "spec", "") or ""
        out |= {m.group(1) for m in _VALUE_RE.finditer(str(spec))}
    return out


# ---------------------------------------------------------------------------
# TEST E — live is the default, demo stays selectable
# ---------------------------------------------------------------------------
def test_e() -> None:
    at = fresh_app()
    check("E1 the app renders with no exception on first load",
          not at.exception, exc_text(at))
    check("E2 first init selects live mode",
          at.session_state["mode"] == "live", str(at.session_state["mode"]))

    mode = widget(at, "radio", "mode")
    check("E3 the mode switch exists in the sidebar", mode is not None)
    # AppTest reports the *formatted* labels (what the operator reads), while
    # .value stays the raw option — so both are checked, not one or the other.
    labels = [str(o) for o in mode.options] if mode is not None else []
    check("E4 both modes are offered, and both are labelled",
          len(labels) == 2 and any("Demo" in x for x in labels)
          and any("Live" in x for x in labels), str(labels))
    check("E5 the switch shows live as the current mode",
          mode is not None and mode.value == "live",
          str(mode.value) if mode is not None else "no widget")

    live_text = page_text(at)
    demo_date = H.BASE_TIME.strftime("%d %b %Y")
    check("E6 live mode never shows the demo story clock",
          demo_date not in live_text, f"found {demo_date!r}")
    check("E7 live mode says it is running on real observations",
          "Real-time observations" in live_text)

    # ---- demo is still one click away -----------------------------------
    at.radio(key="mode").set_value("demo").run()
    check("E8 switching to demo raises nothing", not at.exception, exc_text(at))
    check("E9 demo mode is actually selected",
          at.session_state["mode"] == "demo", str(at.session_state["mode"]))
    check("E10 switching to demo restarts the scenario clock",
          at.session_state["tick"] == 0, str(at.session_state["tick"]))

    demo_text = page_text(at)
    check("E11 demo mode shows the demo story clock, not today's date",
          demo_date in demo_text, f"looked for {demo_date!r}")
    check("E12 demo mode is not presented as live observations",
          "Real-time observations" not in demo_text)
    check("E13 the demo scenario picker appears in demo mode",
          widget(at, "radio", "scenario") is not None)

    # ---- and back again --------------------------------------------------
    at.radio(key="mode").set_value("live").run()
    check("E14 switching back to live raises nothing", not at.exception,
          exc_text(at))
    check("E15 live mode is selected again",
          at.session_state["mode"] == "live", str(at.session_state["mode"]))


# ---------------------------------------------------------------------------
# TEST F — the manual reservoir slider and the 78 % reset button
# ---------------------------------------------------------------------------
def test_f() -> None:
    at = fresh_app()
    check("F0 live mode renders with no exception", not at.exception, exc_text(at))

    sl = widget(at, "slider", "res_pct")
    check("F1 the reservoir slider exists in live mode", sl is not None)
    check("F2 it starts at the seasonal normal (78 %)",
          at.session_state["res_pct"] == 78, str(at.session_state["res_pct"]))
    check("F3 its label names it as manual operator input",
          sl is not None and "manual operator input" in sl.label.lower(),
          sl.label if sl is not None else "no widget")

    text = page_text(at)
    check("F4 the rail heading reads 'Interactive Dam Control'",
          "Interactive Dam Control" in text)
    check("F5 the caption states manual input pending CWC SCADA integration",
          "Manual operator input" in text
          and "IoT telemetry pending CWC SCADA integration" in text)
    check("F6 the panel says the forecast is still computed live",
          "forecast and pre-release are computed live" in text)

    # ---- the slider actually drives the engine ---------------------------
    # The reservoir gauge shows the engine's mass-balance result, not the slider
    # value echoed back, so the figure to look for comes from the engine.
    def expect_pct(frac: float) -> str:
        st_ = H.simulate(H.forcing_from_live(stub_obs(), frac),
                         H.live_tick_for(stub_obs()))
        return f"{round(st_.reservoir_pct, 1)}"

    high_fig, low_fig = expect_pct(0.78), expect_pct(0.41)
    check("F7a the reservoir gauge shows the 78 % result",
          high_fig in gauge_values(at),
          f"looked for {high_fig!r} in {sorted(gauge_values(at))}")

    at.slider(key="res_pct").set_value(41).run()
    check("F7 moving the slider raises nothing", not at.exception, exc_text(at))
    check("F8 the new reading reaches session state",
          at.session_state["res_pct"] == 41, str(at.session_state["res_pct"]))

    moved = gauge_values(at)
    check("F9 the reservoir gauge re-computes from the new reading",
          low_fig in moved and high_fig not in moved,
          f"want {low_fig!r} present and {high_fig!r} gone, got {sorted(moved)}")

    # ---- the reset button ------------------------------------------------
    btn = widget(at, "button", "res_pct_normal_btn")
    check("F10 the 'Set to Seasonal Normal (78%)' button exists", btn is not None)
    check("F11 the button is labelled with the 78 % figure",
          btn is not None and "Seasonal Normal (78%)" in btn.label,
          btn.label if btn is not None else "no widget")

    at.button(key="res_pct_normal_btn").click().run()
    check("F12 clicking the button raises nothing", not at.exception, exc_text(at))
    check("F13 the button restores the seasonal normal",
          at.session_state["res_pct"] == int(round(H.RES_START_FRAC * 100)),
          str(at.session_state["res_pct"]))
    check("F14 the slider itself moved with it",
          widget(at, "slider", "res_pct") is not None
          and widget(at, "slider", "res_pct").value == 78,
          str(getattr(widget(at, "slider", "res_pct"), "value", None)))

    # ---- no widget-state warning anywhere --------------------------------
    warnings = " ".join(str(w.value) for w in at.warning) + \
        " ".join(str(e.value) for e in at.error)
    bad = [phrase for phrase in ("created with a default value",
                                 "also had its value set via the Session State API",
                                 "cannot be modified after the widget")
           if phrase in warnings]
    check("F15 no widget default-vs-session-state warning", not bad, str(bad))


# ---------------------------------------------------------------------------
# TEST H — live selected, but the feed is unreachable
# ---------------------------------------------------------------------------
def test_h() -> None:
    """The failure path must be honest: no dashboard, no invented numbers.

    This is the other half of issue #2 — it is not enough that live is the
    default, the app also has to behave when live cannot be served.
    """
    install_dead_stub()
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    check("H1 an unreachable feed does not raise", not at.exception, exc_text(at))
    check("H2 the app stays in live mode — it never switches for the user",
          at.session_state["mode"] == "live", str(at.session_state["mode"]))

    text = page_text(at)
    check("H3 the unavailable badge is shown",
          "Live data unavailable" in text)
    check("H4 the panel explains that nothing measured is available",
          "Live feed unreachable" in text
          and "nothing measured to show" in text)
    check("H5 it says numbers are withheld rather than estimated",
          "No numbers are displayed" in text)

    keys = widget_keys(at)
    check("H6 a retry button is offered",
          "button:live_retry_now" in keys, str(sorted(keys)))
    check("H7 demo mode is offered as an explicit choice",
          "button:live_fallback_demo" in keys, str(sorted(keys)))

    # The critical assertion: the dashboard must be REPLACED, not decorated.
    leaked = [phrase for phrase in ("Performance at a glance",
                                    "Operational Real-Time Engine Execution",
                                    "Real-time observations")
              if phrase in text]
    check("H8 no dashboard is rendered from fallback values", not leaked,
          f"leaked {leaked}")

    # …and the demo clock must not appear either — that would be the very
    # "demo presented as live" confusion the fix exists to prevent.
    check("H9 the demo story clock does not leak into the failure panel",
          H.BASE_TIME.strftime("%d %b %Y") not in text)

    install_stub()                      # leave the module as we found it


# ---------------------------------------------------------------------------
# TEST I — the Nugen language layer: on every role tab, and still on a budget
# ---------------------------------------------------------------------------
#
# This test used to assert that the layer sent nothing on a page render, because
# the layer was one optional button on the last tab. It is now the product's
# third output for every role — a briefing under each directive card, fetched in
# the background as the basin changes — so "never send anything" is no longer
# the policy. What replaces it is narrower, and is the risk that actually
# matters with a metered endpoint:
#
#   * no key means no call and no crash, on all three role tabs;
#   * the scripted demo never fetches automatically (33 ticks at a 2 s refresh
#     would drain a session budget in under a minute and say nothing new);
#   * in live mode one situation costs at most one call per role, however many
#     times Streamlit re-runs the script;
#   * the automatic allowance is capped strictly below the session budget, so a
#     deliberate briefing is always still available to a reviewer;
#   * the key never reaches the page.
#
# The transport is stubbed throughout, so these are assertions about what the
# app would spend — not about Nugen being reachable from the test machine.
def _nugen_reset() -> None:
    """Zero the layer's cache and both spend counters.

    ``reset_cache`` deliberately leaves the budget alone: it is a session-lifetime
    guard and production code must not be able to clear it. A test that measures
    spend per render has to, so it reaches into the module to do it.
    """
    nugen_client.reset_cache()
    nugen_client._calls_made = 0
    nugen_client._auto_calls = 0
    with nugen_client._lock:
        nugen_client._inflight.clear()


def _stub_nugen() -> dict:
    """Install a local stub transport and return its fresh call counter.

    The stub writes into the real cache under the real key, so the de-duplication
    being measured is the client's own behaviour and not an artefact of the stub.
    Call this at the start of each phase: the returned counter is per-phase, and
    a counter carried across a phase boundary would silently attribute the
    live-by-default first render's calls to whatever is being measured next.

    It does mean ``calls_made()`` stays at zero throughout — that counter lives
    inside the real transport, which is exactly what has been replaced. The
    automatic allowance (``auto_calls_made``) is incremented by the dispatcher
    and is therefore the one these checks read.
    """
    seen: dict = {"n": 0, "roles": []}

    def fake_complete(digest, api_key=None, max_tokens=0, use_cache=True,
                      role=None):
        seen["n"] += 1
        seen["roles"].append(role)
        out = nugen_client.NugenResult(
            ok=True, max_tokens=max_tokens,
            text="Stub briefing for the verify_app run. It prints no figures.")
        with nugen_client._lock:
            nugen_client._cache[
                nugen_client._cache_id(digest, max_tokens, role)] = out
        return out

    nugen_client.complete = fake_complete
    return seen


def _drain(seconds: float = 15.0) -> bool:
    """Wait for the background briefing threads to land."""
    deadline = time.time() + seconds
    while nugen_client.pending_count() and time.time() < deadline:
        time.sleep(0.05)
    return nugen_client.pending_count() == 0


def test_i() -> None:
    for var in ("NUGEN_API_KEY", "HYDRO_NUGEN_KEY"):
        os.environ.pop(var, None)

    real_complete = nugen_client.complete
    _nugen_reset()
    seen = _stub_nugen()
    try:
        # ---- no key: the layer announces itself and costs nothing ---------
        at = fresh_app()
        check("I1 the app renders with no key configured", not at.exception,
              exc_text(at))
        text = page_text(at)
        check("I2 the panel presents the layer as the briefings on the role tabs",
              "Nugen language layer" in text
              and "Every role tab carries a Nugen briefing" in text)
        check("I3 it states the layer is off, and how to switch it on",
              "Briefing layer is off" in text and "NUGEN_API_KEY" in text)
        check("I4 no generate button exists without a key",
              "button:nugen_go" not in widget_keys(at))
        check("I5 all three role tabs say the layer is off, in one line each",
              text.count("layer is off, no") == 3,
              f"found {text.count('layer is off, no')} of 3")
        check("I6 no key means nothing was sent", seen["n"] == 0,
              f"{seen['n']} call(s)")

        # ---- with a key, demo mode: still nothing automatic ---------------
        # Live is the default, so the app has to be *switched* to demo first,
        # and that first live render legitimately fetches. The counters are
        # therefore reset after the switch, so what is measured below is one
        # demo render and nothing else.
        os.environ["NUGEN_API_KEY"] = "verify-app-not-a-real-key"
        at = fresh_app()
        at.radio(key="mode").set_value("demo").run()
        check("I7 demo mode renders with a key configured", not at.exception,
              exc_text(at))
        _drain()
        _nugen_reset()
        seen = _stub_nugen()
        at.run()
        _drain(2.0)
        check("I8 the demo never fetches a briefing automatically",
              seen["n"] == 0 and nugen_client.auto_calls_made() == 0,
              f"{seen['n']} call(s), {nugen_client.auto_calls_made()} charged")

        # ---- live mode: one call per role, then free ----------------------
        _nugen_reset()
        seen = _stub_nugen()
        at = fresh_app()
        check("I9 live mode renders with a key configured", not at.exception,
              exc_text(at))
        check("I10 the layer's actual spend is reported, not promised",
              "Spent so far this session" in page_text(at))
        check("I11 the background briefings land", _drain(),
              f"{nugen_client.pending_count()} still in flight")
        check("I12 one automatic call per role, and no more",
              sorted(r or "" for r in seen["roles"])
              == ["dam", "disaster", "farmer"], str(seen["roles"]))
        check("I13 all three were charged to the automatic allowance",
              nugen_client.auto_calls_made() == 3
              and nugen_client.auto_budget_left()
              == nugen_client.AUTO_CALL_BUDGET - 3,
              f"{nugen_client.auto_calls_made()} charged, "
              f"{nugen_client.auto_budget_left()} of "
              f"{nugen_client.AUTO_CALL_BUDGET} left")

        at.run()
        text = page_text(at)
        check("I14 the briefing is then on the page for each role",
              text.count("Stub briefing for the verify_app run") == 3,
              f"found {text.count('Stub briefing for the verify_app run')} of 3")
        at.run()
        _drain(2.0)
        check("I15 re-rendering an unchanged basin spends nothing further",
              seen["n"] == 3, f"{seen['n']} call(s) after three renders")

        check("I16 the automatic allowance is capped below the session budget",
              0 < nugen_client.AUTO_CALL_BUDGET < nugen_client.CALL_BUDGET,
              f"{nugen_client.AUTO_CALL_BUDGET} of {nugen_client.CALL_BUDGET}")
        check("I17 the key itself is never rendered into the page",
              "verify-app-not-a-real-key" not in text)
    finally:
        nugen_client.complete = real_complete
        os.environ.pop("NUGEN_API_KEY", None)
        _nugen_reset()


def test_j() -> None:
    """The cloud-host case: the SERVER's fetch fails, the browser's is in flight.

    On Render the app's own Open-Meteo call is routinely refused on the shared
    datacentre IP while the visitor's in-browser fetch succeeds a moment later.
    The first render must therefore say it is still *fetching* — claiming the
    feed is unreachable while a fetch is in flight makes a working console look
    broken — and a miss that survives a refresh cycle must still be reported
    honestly, with the retry.
    """
    os.environ["HYDRO_BROWSER_LIVE"] = "1"          # arm the in-browser bridge
    try:
        install_dead_stub()
        at = AppTest.from_file(APP, default_timeout=TIMEOUT)
        at.run()

        check("J1 the first render with the bridge armed does not raise",
              not at.exception, exc_text(at))
        text = page_text(at)
        check("J2 it says the reading is still being fetched",
              "Fetching the first reading" in text)
        check("J3 it does not yet claim the feed is unreachable",
              "Live feed unreachable" not in text)
        check("J4 it is explicit that nothing stands in for the missing reading",
              "no estimated numbers stand in for it" in text)
        leaked = [p for p in ("Performance at a glance",
                              "Operational Real-Time Engine Execution",
                              "Real-time observations") if p in text]
        check("J5 no dashboard is rendered while connecting", not leaked,
              f"leaked {leaked}")

        # …and a miss that outlives the grace is reported as a failure.
        at.run()
        text2 = page_text(at)
        check("J6 a second miss is reported as unreachable",
              "Live feed unreachable" in text2)
        check("J7 the retry is offered then",
              "button:live_retry_now" in widget_keys(at), str(sorted(widget_keys(at))))
        check("J8 the reason from the feed is surfaced for diagnosis",
              "simulated network failure" in text2)
    finally:
        os.environ["HYDRO_BROWSER_LIVE"] = "0"
        install_stub()


def main() -> int:
    for fn in (test_e, test_f, test_h, test_i, test_j):
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
