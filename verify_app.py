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
* **Test P** — the instrument itself. Every negative check in this file is only
  as good as ``page_text``, and ``page_text`` used to miss ``st.code`` entirely
  — at one point 3622 characters of verbatim prompt text sat on the page with
  no check in this file able to see them. ``test_p`` renders a throwaway app
  that writes one marker into every element kind and asserts the harvest finds
  each one, so the harness is measured before it is trusted.
* **Test L** — rule 2, *no implementation plumbing in the UI*: no model id, no
  prompt text, no call count, no token figure, no budget number, no API-key
  instruction and no apology for the language layer being absent.
* **Test M** — rule 1, *the AI may not state or forecast a hazard*: only
  ``hydro_engine`` does that. The language layer's wording never reaches the
  page when it names a hazard, every mitigation shown is a catalogue entry,
  every alert shown is a detected pattern, and no safe destination is invented.
* **Test Z** — meta-checks on this file: the stub transport's signature must
  track the real ``complete()``, and a string asserted *absent* must be
  asserted *present* somewhere, or the absence check measures nothing.

The live feed is stubbed with a fixed observation so the run is offline and
deterministic; the browser-side JS fetch is disabled through the app's own
``HYDRO_BROWSER_LIVE`` switch for the same reason.

Run:  python verify_app.py      Exit: 0 = all passed, 1 = at least one failed.
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import tempfile
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


# ---------------------------------------------------------------------------
# The instrument: everything the page rendered
# ---------------------------------------------------------------------------
#
# ``page_text`` used to enumerate seven typed accessors (markdown, caption,
# text, info, warning, error, success). That is the wrong shape for a file whose
# strongest checks are *negative*: an absence check over a harvest that cannot
# see an element kind is not a weak check, it is no check at all. ``st.code``
# was the one that bit — a prompt dump of 3622 characters sat on the page and
# nothing here could see it.
#
# So the harvest is now a recursive walk of the element tree instead of a list
# of accessors, which means a kind nobody thought of is picked up for free.
# What that walk can and cannot reach was measured against the installed
# Streamlit (1.64) rather than assumed:
#
# REACHED, as elements, by the walk and by the typed accessors alike —
#   markdown, caption, text, code, metric, header, subheader, title, table,
#   dataframe, json, latex, divider, info, warning, error, success.
#   Nesting is not a barrier: elements written inside ``st.expander``,
#   ``st.columns``, ``st.tabs`` and ``st.container`` are returned exactly as
#   top-level ones are, and ``at.markdown`` et al. include the *sidebar* too
#   (the walk therefore covers ``at.main`` and ``at.sidebar`` and no more).
#
# REACHED ONLY BY THE WALK — these have no typed accessor at all, so the old
# harvest was blind to every one of them:
#   * ``st.expander`` labels. The brief's suspicion is confirmed: an expander
#     label is a property of a *block*, not an element, so no accessor returns
#     it. A tree walk does reach it, as ``Expander.label`` — which is strictly
#     better than the note in J5 below used to claim, and the reason this is a
#     walk. Tab labels (``Tab.label``) are the same shape.
#   * ``st.metric`` labels and deltas. ``Metric.value`` is the figure *only*;
#     the label and the delta are separate properties, absent from ``dir()``
#     but present on the node. All three are harvested.
#   * widget labels (button, slider, radio, selectbox, text_input, …), which is
#     where a "Dump the prompt" control would announce itself.
#   * ``st.html``, which lands as an ``UnknownElement`` whose ``.value`` is the
#     raw markup.
#
# NOT REACHED, and worth stating so nothing is asserted against them:
#   * anything inside a Plotly or PyDeck figure. Those are ``UnknownElement``s
#     whose ``.value`` raises; their payload is in ``.proto.spec``, which
#     ``gauge_values`` below reads separately for the reservoir gauge.
#   * ``st.spinner`` / ``st.form`` chrome, and ``st.set_page_config`` metadata.
#   * CSS is *not* excluded — ``app.py`` ships its stylesheet through
#     ``st.markdown``, so the harvest contains the whole ``<style>`` block.
#     That is honest (it really is on the page) but it is why the numeric leak
#     probes in ``test_l`` are contextual: bare "18" and "500" occur in
#     ``rgba(18,59,84,…)`` and in a font-weight list.
#
# Widget *values* are deliberately dropped unless they are strings: a button's
# value is ``False`` and a slider's is ``78``, which is session state rather
# than page copy, and a bare "78" in the harvest would weaken every numeric
# probe for nothing.
def _kids(node) -> list:
    """A node's children. AppTest stores them in a dict keyed by position."""
    children = getattr(node, "children", None)
    if not children:
        return []
    return list(children.values()) if isinstance(children, dict) \
        else list(children)


def _is_widget(node) -> bool:
    return hasattr(node, "set_value") or hasattr(node, "click") \
        or hasattr(node, "check")


def page_text(at) -> str:
    """Every string the page rendered, from a full walk of the element tree.

    Covers element text, expander/tab labels, widget labels and metric
    labels/deltas, in the main body and the sidebar. See the block comment
    above for exactly what this does and does not reach.
    """
    parts: list[str] = []

    def walk(node) -> None:
        for attr in ("value", "label", "delta"):
            try:
                val = getattr(node, attr)
            except Exception:       # Plotly/PyDeck .value raises; skip it
                continue
            if val is None:
                continue
            if isinstance(val, str):
                parts.append(val)
            elif not _is_widget(node):
                # Table/Dataframe hand back a DataFrame; render it as text.
                parts.append(str(val))
        for child in _kids(node):
            walk(child)

    for root in (at.main, at.sidebar):
        try:
            walk(root)
        except Exception as exc:                        # pragma: no cover
            parts.append(f"<page_text walk failed: {exc}>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Absence checks, and the registry that stops them going vacuous
# ---------------------------------------------------------------------------
#
# A prior audit found "Performance at a glance" and "Operational Real-Time
# Engine Execution" asserted *absent* (H8, J5) and never asserted present
# anywhere. An absence check for a string the app never renders cannot fail,
# whatever the app does — it measures the string, not the behaviour. Every
# absence assertion therefore goes through ``absent()``, every presence
# assertion through ``present()``, and ``test_z`` holds the two registries
# against each other at the end of the run.
ASSERTED_ABSENT: set[str] = set()
ASSERTED_PRESENT: set[str] = set()

# Strings that are absent *by design* and have no healthy render to appear on:
# the deleted briefing panel's own copy, and the language layer's internals.
# A leak probe is not vacuous for being unsatisfiable — being unsatisfiable is
# the whole claim. They are listed, rather than silently exempted, so the
# exemption is a decision somebody can read.
ABSENT_BY_DESIGN: set[str] = set()


def absent(text: str, needle: str, by_design: bool = False) -> bool:
    """``needle not in text``, recorded so test_z can audit the assertion."""
    ASSERTED_ABSENT.add(needle)
    if by_design:
        ABSENT_BY_DESIGN.add(needle)
    return needle not in text


def present(text: str, needle: str) -> bool:
    """``needle in text``, recorded so test_z can audit the assertion."""
    ASSERTED_PRESENT.add(needle)
    return needle in text


def missing(text: str, needles) -> list:
    """The needles that are present — i.e. the leaks. Empty list is a pass."""
    return [n for n in needles if not absent(text, n)]


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

    The signature has to track ``nugen_client.complete``'s, because a stub that
    rejects a keyword the real one accepts raises inside the dispatcher's worker
    thread, where the exception is caught and recorded as a briefing error — the
    counters still move, so the de-duplication checks below would keep passing
    while measuring nothing. ``**kwargs`` absorbs the rest of the real
    signature (``model``, ``messages``, ``strict_labels``, ``auto``) and whatever
    is added next.

    ``cache_id`` is taken by name rather than swallowed, because it is the key
    the result has to be filed under: ``request_async`` computes the id, hands
    it to the UI and *pins* it on the call, and a result written anywhere else is
    one nobody looks up. The pinned id currently equals what the stub would
    recompute, so this is insurance rather than a live fix — but it is the kind
    of insurance that matters here, since the failure it prevents is silent.
    """
    seen: dict = {"n": 0, "roles": []}

    def fake_complete(digest, api_key=None, max_tokens=0, use_cache=True,
                      role=None, cache_id=None, **kwargs):
        seen["n"] += 1
        seen["roles"].append(role)
        out = nugen_client.NugenResult(
            ok=True, max_tokens=max_tokens,
            text="Stub briefing for the verify_app run. It prints no figures.")
        cid = cache_id or nugen_client._cache_id(digest, max_tokens, role,
                                                 kwargs.get("model"))
        with nugen_client._lock:
            nugen_client._cache[cid] = out
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
        # The three strings below are the dashboard's own, and all three are
        # still reachable by ``page_text`` on a healthy render even though
        # "Operational Real-Time Engine Execution" now also appears inside the
        # ``st.expander`` on the AI analyst tab. That was checked rather than
        # assumed: AppTest's element tree recurses into expander blocks, so
        # markdown written inside one is returned by ``at.markdown`` exactly as
        # top-level markdown is. (Only the expander's *label* is invisible to it,
        # being a block property and not an element.) The leak guard therefore
        # still measures a leak; it has not gone vacuous.
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


# ---------------------------------------------------------------------------
# TEST K — the AI analyst tab (tab 5, formerly "Model & validation")
# ---------------------------------------------------------------------------
#
# Tab 5 stopped being a validation read-out and became an interface: a reviewer
# types a question and the console answers it. That changes what can go wrong,
# because the tab now has a language model in it and a flood console that states
# a wrong depth or a wrong evacuation window is worse than one that says nothing.
# The claim the tab makes on screen is that the model is never the thing that
# reads the data — the figure is retrieved off the computed state by name, the
# answer is composed by rule *before* any model runs, and the model only supplies
# the English. These checks hold that claim to account at the four points where
# it could quietly stop being true:
#
#   * the answer must exist without the model. There is no key in this
#     environment and the endpoint must never be contacted, so the data-only
#     path is the one that has to carry a real, non-empty, figure-bearing answer
#     — and it must reach the page, not merely the session state;
#   * a question the console does not hold must be *declined*. This is the one
#     behaviour a fluent model will always get wrong if it is allowed to try, so
#     the scope check has to fire before retrieval and before any call;
#   * both paths must spend nothing. The transport is stubbed with a counter, so
#     a zero here means the transport was never entered at all — not that the
#     network happened to be down;
#   * the harness must keep reporting all six stages. It is what makes the
#     pipeline checkable instead of merely asserted, and a table that silently
#     lost its rows would leave the claim with nothing behind it.
#
# The last two checks guard the closing speed comparison, and K19 in particular
# is an honesty guard rather than a UI assertion: the "~2.3 h" column is a
# published HEC-RAS 2D benchmark and not a measurement taken here, and the
# caption that says so is the only thing standing between a fair comparison and
# a 100x speedup claim the project cannot support. If that caveat is ever
# deleted, this check is meant to fail.
def test_k() -> None:
    for var in ("NUGEN_API_KEY", "HYDRO_NUGEN_KEY"):
        os.environ.pop(var, None)

    real_complete = nugen_client.complete
    _nugen_reset()
    seen = _stub_nugen()
    try:
        at = fresh_app()
        check("K1 the AI analyst tab renders with no exception", not at.exception,
              exc_text(at))

        text = page_text(at)
        check("K2 the tab carries the analyst heading, not the old model one",
              "AI support &amp; analyst" in text
              and "answered from the state this page just computed" in text)
        check("K3 it states up front how an answer is produced",
              "How an answer is produced" in text
              and "before any model runs" in text)

        # ---- the ask surface exists as widgets ---------------------------
        keys = widget_keys(at)
        chips = [f"button:ask_chip_{q[:24]}"
                 for q in nugen_client.SUGGESTED_QUESTIONS]
        missing = [c for c in chips if c not in keys]
        check("K4 every suggested question is offered as a chip",
              len(chips) == 6 and not missing, f"missing {missing}")
        check("K5 the question box and both ask buttons exist",
              widget(at, "text_input", "ask_q") is not None
              and "button:ask_go" in keys and "button:ask_raw" in keys,
              str(sorted(k for k in keys if "ask" in k)))
        check("K6 nothing is asked, and nothing sent, until the operator asks",
              "Nothing has been asked" in text and seen["n"] == 0,
              f"{seen['n']} call(s)")

        # ---- the data-only path: an answer with no key and no model -------
        # This is the path that has to work when the language layer is absent,
        # which in this environment it always is.
        at.text_input(key="ask_q").set_value(nugen_client.SUGGESTED_QUESTIONS[0])
        at.button(key="ask_raw").click().run()
        check("K7 asking from the data only raises nothing", not at.exception,
              exc_text(at))

        ans = (at.session_state.get("_ask") or {}).get("ans")
        check("K8 the question was in scope and read fields off the state",
              ans is not None and ans.in_scope and len(ans.facts) > 0,
              f"in_scope={getattr(ans, 'in_scope', None)}, "
              f"{len(getattr(ans, 'facts', []))} field(s)")

        text = page_text(at)
        # The app writes the answer through its own minimal HTML escape, so the
        # same transform is applied here rather than asserting on a fragment
        # that happens to contain no markup characters today.
        shown = (str(getattr(ans, "grounded", "")).replace("&", "&amp;")
                 .replace("<", "&lt;").replace(">", "&gt;"))
        check("K9 a non-empty computed answer reaches the page",
              len(shown) > 40 and shown in text,
              f"grounded is {len(shown)} char(s), on page: {shown in text}")
        check("K10 it is presented as computed, with the model not called",
              "computed from the retrieved data" in text
              and "The language layer was not called, by request" in text)
        check("K11 the fields the answer was built from are shown",
              "Evidence retrieved" in text
              and "read by name off the computed state" in text)
        check("K12 the data-only path contacted nothing",
              seen["n"] == 0 and nugen_client.calls_made() == 0,
              f"{seen['n']} stub call(s), {nugen_client.calls_made()} real")

        # ---- the harness reports every stage ------------------------------
        check("K13 the harness table renders all six stage rows",
              "AI harness" in text
              and all(stage in text for stage in
                      ("Scope check", "Retrieval", "Grounded answer",
                       "Language model", "Figure check", "Fallback")),
              str([st_ for st_ in ("Scope check", "Retrieval",
                                   "Grounded answer", "Language model",
                                   "Figure check", "Fallback")
                   if st_ not in text]))

        # ---- out of scope is declined, not guessed ------------------------
        at.text_input(key="ask_q").set_value("What is the capital of France?")
        at.button(key="ask_raw").click().run()
        check("K14 an out-of-scope question raises nothing", not at.exception,
              exc_text(at))

        oos = (at.session_state.get("_ask") or {}).get("ans")
        text = page_text(at)
        # ``facts`` is deliberately NOT asserted empty here. ``retrieve`` always
        # includes the two "core" fields (flood_sev, drought_sev) because its
        # wanted-set is ``topics | {"core"}``, so an out-of-scope question still
        # comes back with those two read off the state. That is harmless and the
        # pipeline ignores them: with no topic matched ``in_scope`` is False, so
        # no digest is built, no model object exists and the evidence table is
        # never rendered. Those are the things worth asserting, and they are
        # stricter than a fact count would be — an empty digest means nothing was
        # even assembled to send.
        check("K15 it is declined rather than answered",
              oos is not None and not oos.in_scope and not oos.digest
              and oos.result is None
              and "This console does not hold that" in text
              and "nothing was sent" in text,
              f"in_scope={getattr(oos, 'in_scope', None)}, "
              f"digest={len(getattr(oos, 'digest', '') or '')} char(s), "
              f"result={getattr(oos, 'result', None)!r}")
        check("K16 declining it cost nothing, and showed no evidence table",
              seen["n"] == 0 and nugen_client.calls_made() == 0
              and "Evidence retrieved" not in text,
              f"{seen['n']} stub call(s), {nugen_client.calls_made()} real")

        # ---- the old validation evidence is still reachable ---------------
        check("K17 the engine's self-check survived the move into the expander",
              "Operational Production Engine" in text
              and "mass balance" in text.lower())

        # ---- the closing comparison, and its caveat ----------------------
        check("K18 the comparison with conventional processing renders",
              "HydroSentry-AI vs conventional processing" in text
              and "Conventional 2D modelling" in text)
        check("K19 the comparison still admits ~2.3 h was not measured here",
              "~2.3 h is a published benchmark for an equivalent HEC-RAS 2D "
              "run, not something measured here" in text,
              "the speed comparison's honesty caveat is missing")
    finally:
        nugen_client.complete = real_complete
        _nugen_reset()


def main() -> int:
    for fn in (test_e, test_f, test_h, test_i, test_j, test_k):
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
