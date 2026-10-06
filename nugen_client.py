"""
nugen_client.py — the plain-language briefing layer (Nugen inference API)
==============================================================================

WHAT THIS IS
------------
A thin, defensive client for the Nugen chat-completions endpoint. It does two
jobs, and the second one is new:

* **Briefings.** It takes the directives the deterministic rule engine has
  *already* produced and asks a small hosted instruct model to re-word them for
  one audience at a time: the farmer, the dam duty engineer, and the district
  disaster officer each get the same decision in the register they actually
  read.
* **The analyst.** It answers an operator's own question from fields selected by
  name off the computed state — never from the model's memory. See the big
  section header at the bottom of this file for the pipeline and why it is built
  that way round.

Model ids are resolved from the account's own catalogue at first call (see
``resolve_models``); ``Qwen/Qwen2.5-0.5B-Instruct`` is the documented fallback,
and ``known_models()`` reports the two ids actually in force.

This is a first-class layer of the product, not a demo toggle. It is wired into
all three operator tabs and refreshes itself as the basin changes.

WHAT THIS IS NOT
----------------
It is **not** part of the forecast. It never computes, predicts or changes a
number — every figure it is allowed to mention is handed to it in the prompt,
and if the call fails, is not configured, or is out of budget, the console is
unaffected: the rule-based directives are the product, and the directive cards
stay on screen either way. That separation is the point, not a limitation: a
small model is a good writer and a bad hydrologist, so it is given the writing
and kept away from the hydrology.

QUOTA DISCIPLINE (the account allows 500 tokens per completion)
---------------------------------------------------------------
* ``MAX_TOKENS_LIMIT = 500`` is enforced with ``min()`` — a caller cannot
  request more, whatever it passes.
* The default request is smaller still (``DEFAULT_MAX_TOKENS``) because an
  operator briefing is a few sentences, not an essay. A single-role briefing is
  smaller again (``ROLE_MAX_TOKENS``).
* The prompt is built from a compact digest of the state and hard-truncated
  (``_PROMPT_CHAR_LIMIT``) so input tokens stay bounded too. A role digest
  carries only that role's own facts, so it is cheaper than the combined one.
* Results are cached in-process on a hash of the exact prompt, so the 60-second
  live refresh re-renders the briefing for free instead of re-billing it.
* ``CALL_BUDGET`` caps how many *network* calls this process will ever make, so
  a redeploy loop or a stuck refresh cannot drain the quota.
* Automatic refreshes come through ``request_async``, which is keyed on the
  prompt hash and therefore spends at most one call per distinct situation no
  matter how many times the page re-renders.

WHY ``request_async`` EXISTS
----------------------------
The transport is a synchronous ``requests.post`` with a 30-second read timeout.
Calling that inline from a Streamlit rerun would freeze the dashboard for as
long as the endpoint takes to answer — the console would appear to hang on
exactly the updates that matter most. ``request_async`` instead hands the call
to a worker thread and returns immediately; the UI reads the result on a later
rerun, which the live refresh already provides for free. Results live in a
module-level dict rather than ``st.session_state`` because a worker thread
cannot safely touch session state.

SECRETS
-------
The key is read from the ``NUGEN_API_KEY`` environment variable (or passed in by
the caller, which is how the app forwards ``st.secrets``). It is never written
to the repository. On Render, set it under *Environment → Environment Variables*.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass, field

import requests

# ---------------------------------------------------------------------------
# Endpoint / budget configuration
# ---------------------------------------------------------------------------
ENDPOINT = "https://api.nugen.in/api/v3/inference/chat/completions"
MODELS_ENDPOINT = "https://api.nugen.in/api/v3/models/base"

# The endpoint addresses a base model by its EXACT Hugging Face id. A short
# vendor-style alias (``qwen-v2p5-0p5b-instruct``) is answered with
#
#   404 {"detail": "unable to resolve model '...'. Base models are called by
#        their exact Hugging Face id, e.g. 'Qwen/Qwen2.5-0.5B-Instruct', and
#        must be inference_ready in GET /models/base."}
#
# so the default below is the exact id, and ``resolve_models`` checks it against
# the account's own catalogue before the first call. See the model-resolution
# section for why there are two of them.
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"

MAX_TOKENS_LIMIT = 500      # hard account cap — never exceeded
DEFAULT_MAX_TOKENS = 220    # a briefing is short; stay well inside the cap
TEMPERATURE = 0.1           # near-deterministic: this is a re-wording task
TIMEOUT = (5, 30)           # (connect, read) seconds
# Max network calls per process. The automatic role briefings take at most
# AUTO_CALL_BUDGET of these; the rest is headroom for the things a human asks
# for on purpose — the combined briefing, and the analyst's questions. The
# analyst is interactive, so that headroom has to be big enough to hold a
# conversation in front of a reviewer rather than two questions.
CALL_BUDGET = 60
_PROMPT_CHAR_LIMIT = 2400   # ~600 input tokens

_ENV_KEYS = ("NUGEN_API_KEY", "HYDRO_NUGEN_KEY")

_lock = threading.Lock()
_cache: dict[str, "NugenResult"] = {}
_calls_made = 0


@dataclass
class NugenResult:
    """Outcome of one briefing request. Always returned — never raised."""
    ok: bool = False
    text: str = ""
    error: str = ""
    cached: bool = False
    model: str = MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    usage: dict = field(default_factory=dict)
    # Figures in the reply that do not appear in the facts it was given.
    unsupported: list = field(default_factory=list)

    @property
    def tokens_out(self) -> int:
        return int(self.usage.get("completion_tokens") or 0)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def default_api_key() -> str:
    """The key from the environment, or '' when the layer is not configured."""
    for name in _ENV_KEYS:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


def is_configured(api_key: str | None = None) -> bool:
    return bool((api_key or default_api_key()).strip())


# ---------------------------------------------------------------------------
# Model resolution
#
# A hard-coded model id is correct only until the account's catalogue changes,
# and the failure mode is a 404 on every call — which is how this layer broke
# once already. Two defences, in order:
#
#   1. ``MODEL`` is the exact Hugging Face id the API's own error message names,
#      so the layer works even if discovery never runs.
#   2. ``resolve_models`` reads ``GET /models/base?inference_ready=true`` once
#      per process and picks from what this account can actually call. That
#      listing is not an inference request, so it is not charged against
#      ``CALL_BUDGET``.
#
# Two ids are chosen, because the layer now does two different jobs:
#
#   * the BRIEFING model re-words one directive into three sentences. The
#     smallest instruct model in the catalogue is the right tool for that and
#     the cheapest.
#   * the ANALYST model answers an operator's question from a retrieved fact
#     sheet. That needs comprehension rather than paraphrase, so the largest
#     model that still answers inside the read timeout is preferred.
#
# Both fall back to ``MODEL``, so an unreachable or empty catalogue costs
# nothing but the larger model.
# ---------------------------------------------------------------------------
_ANALYST_MAX_B = 34.0       # above this, a 30 s read timeout is a real risk

_models_resolved = False
_briefing_model = MODEL
_analyst_model = MODEL
_catalogue: list[dict] = []
_catalogue_note = "not read yet"

_SIZE_RE = re.compile(r"([0-9]*\.?[0-9]+)\s*([BM]?)", re.I)


def _params_b(entry: dict) -> float:
    """Parameter count in billions from a catalogue entry's '0.49B' / '7B'.

    An unparseable size returns ``inf``, which sorts last when picking the
    smallest and is excluded by the analyst's ceiling — an unknown model is
    never silently promoted into the slot where latency matters.
    """
    mt = _SIZE_RE.match(str(entry.get("parameters") or "").strip())
    if not mt:
        return float("inf")
    val = float(mt.group(1))
    return val / 1000.0 if mt.group(2).upper() == "M" else val


def _usable(entry: dict) -> bool:
    """A catalogue row this app may actually send a chat completion to."""
    return (bool(entry.get("model_id"))
            and bool(entry.get("inference_ready"))
            and not entry.get("available_on_request")
            and str(entry.get("type") or "text-generation") == "text-generation")


def resolve_models(api_key: str | None = None,
                   force: bool = False) -> tuple[str, str]:
    """``(briefing_model, analyst_model)`` for this account. Never raises.

    Reads the catalogue at most once per process unless ``force`` is set, which
    is how a 404 from the chat endpoint asks for a second opinion. Any failure
    leaves both ids at ``MODEL`` and records why in ``catalogue_note``.
    """
    global _models_resolved, _briefing_model, _analyst_model
    global _catalogue, _catalogue_note

    with _lock:
        if _models_resolved and not force:
            return _briefing_model, _analyst_model

    key = (api_key or default_api_key()).strip()
    rows: list[dict] = []
    if not key:
        note = "no API key, so the catalogue was never read"
    else:
        try:
            resp = requests.get(
                MODELS_ENDPOINT,
                params={"inference_ready": "true", "type": "text-generation",
                        "limit": 100},
                headers={"accept": "application/json",
                         "Authorization": f"Bearer {key}"},
                timeout=TIMEOUT)
            if resp.status_code != 200:
                note = (f"GET /models/base returned HTTP {resp.status_code} — "
                        f"falling back to {MODEL}")
            else:
                rows = [e for e in (resp.json().get("models") or [])
                        if isinstance(e, dict) and _usable(e)]
                note = (f"{len(rows)} inference-ready text model"
                        f"{'' if len(rows) == 1 else 's'} on this account"
                        if rows else
                        f"the catalogue listed no inference-ready text model — "
                        f"falling back to {MODEL}")
        except (requests.RequestException, ValueError) as exc:
            note = f"could not read the catalogue ({type(exc).__name__}) — falling back to {MODEL}"

    # An instruction-tuned checkpoint is required: a raw base model continues
    # text rather than following a system prompt, which would make every
    # briefing unusable. If the account has none, prefer the documented default
    # over guessing with a completion-only model.
    chat = [e for e in rows if any(tag in str(e["model_id"]).lower()
                                   for tag in ("instruct", "chat", "-it"))]
    brief_m = analyst_m = MODEL
    if chat:
        by_size = sorted(chat, key=_params_b)
        brief_m = str(by_size[0]["model_id"])
        fits = [e for e in by_size if _params_b(e) <= _ANALYST_MAX_B]
        analyst_m = str((fits or by_size)[-1]["model_id"])
    elif rows:
        note += " (none instruction-tuned)"

    with _lock:
        _catalogue, _catalogue_note = rows, note
        _briefing_model, _analyst_model = brief_m, analyst_m
        _models_resolved = True
    return brief_m, analyst_m


def known_models() -> tuple[str, str]:
    """The ids in force right now. Never blocks and never fetches.

    The UI labels every card with the model that produced it and is re-rendered
    far more often than it calls the endpoint, so it must not be the thing that
    triggers a catalogue read — a 5-second connect timeout on the first paint is
    exactly the frozen-first-load this app has fixed elsewhere. Resolution
    happens inside ``complete``, which already runs off the render thread.
    """
    with _lock:
        return _briefing_model, _analyst_model


def catalogue_note() -> str:
    """One line on where the active ids came from — for the audit panel."""
    with _lock:
        return _catalogue_note


def calls_made() -> int:
    """Network calls this process has spent (cache hits are not counted)."""
    return _calls_made


def budget_left() -> int:
    return max(0, CALL_BUDGET - _calls_made)


def reset_cache() -> None:
    """Drop cached briefings (the call budget is deliberately NOT reset).

    Also drops the recorded outcomes of background requests (``_async_out``,
    defined with the non-blocking path below), so a cleared cache really does
    mean "ask again" rather than replaying a stale error. In-flight requests are
    left alone — they will land in a cache nobody is waiting on, which is
    harmless, and cancelling a thread mid-stream is not.
    """
    with _lock:
        _cache.clear()
        _async_out.clear()


# ---------------------------------------------------------------------------
# Prompt construction
#
# Shape matters more than wording on a small model. Three shapes were tried live
# against this endpoint before settling here:
#   * instruction + facts in one user turn  -> drifts into a "- - -" loop;
#   * system instruction + facts only       -> echoes the facts back verbatim;
#   * system + ONE worked example + facts   -> clean, correctly-numbered prose.
# So the request carries a single fixed example (about 180 input tokens, paid
# once per call and never regenerated) and the live facts as the final turn.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a river-basin flood control room briefing officer. Rewrite the "
    "facts the user gives you as a short duty-officer briefing of three or four "
    "plain-English sentences. Use only the numbers given. Never add, estimate or "
    "change a number, and never invent an instruction."
)

_EXAMPLE_FACTS = (
    "Basin: Krishna Basin (Sangli). Time: 02 Aug 2026, 09:00 IST.\n"
    "Flood: WATCH. Peak inflow 410 m3/s expected in 6.0 h. Reservoir 64% full. "
    "Recommended release 150 m3/s.\n"
    "Drought: safe. Evaporative stress percentile 55.\n"
    "Dam order: Hold normal release and ready the gates.\n"
    "Disaster order: No evacuation required, keep teams on standby.\n"
    "Farmer order: No irrigation action needed."
)
_EXAMPLE_BRIEFING = (
    "Inflow is rising towards 410 m3/s in about six hours, with the reservoir at "
    "64% and release held at 150 m3/s. The dam team should keep the normal "
    "release and have the gates ready. No evacuation is needed, but response "
    "teams stay on standby. Farmers can keep their usual irrigation schedule."
)

# ---------------------------------------------------------------------------
# Per-role briefings
#
# The console already states the decision once, authoritatively, in each role
# tab's directive card. What it cannot do is say it twice — once as an order and
# once in the register the reader actually uses. A duty engineer wants release
# rates; a farmer wants to know whether to irrigate this week; a disaster officer
# wants minutes and sectors. Same decision, three readers.
#
# Each role therefore gets its own instruction and its own worked example, in the
# same three-turn shape proven above. The example is what keeps a small model from
# echoing the facts back, so it is not optional — but it is also the bulk of the
# input cost, which is why ROLE_MAX_TOKENS is small and the role digests below
# carry only the facts that role is allowed to mention.
# ---------------------------------------------------------------------------
ROLE_MAX_TOKENS = 150       # two or three sentences for one reader

ROLES: dict[str, dict] = {
    "farmer": {
        "label": "For the farmer",
        "system": (
            "You are an agricultural extension officer speaking to a smallholder "
            "farmer. Rewrite the facts as two or three short, plain sentences "
            "about water and the crop. No jargon, no percentages the facts do "
            "not give you. Use only the numbers given, never add or change one, "
            "and never invent an instruction."
        ),
        "example_facts": (
            "Basin: Krishna Basin (Sangli).\n"
            "Drought: watch. Evaporative stress percentile 38. Soil moisture "
            "0.19 m3/m3. Days to wilting 6. Rain now 0.0 mm/hr.\n"
            "Farmer order: Begin deficit irrigation. Irrigate at 60% of normal "
            "depth on a 3-day cycle."
        ),
        "example_briefing": (
            "The soil is drying faster than the crop can draw on it, and at this "
            "rate the root zone reaches wilting point in about 6 days. Start "
            "irrigating at 60% of your normal depth, once every three days, "
            "rather than waiting for the crop to show stress. No rain is falling "
            "now, so do not count on it to make up the shortfall."
        ),
    },
    "dam": {
        "label": "For the duty engineer",
        "system": (
            "You are writing the shift note for the duty engineer in a reservoir "
            "control room. Rewrite the facts as two or three short sentences: "
            "what the inflow is doing, what the storage is, and what to do with "
            "the gates. Use only the numbers given, never add or change one, and "
            "never invent an instruction."
        ),
        "example_facts": (
            "Basin: Krishna Basin (Sangli).\n"
            "Flood: WARNING. Peak inflow 820 m3/s expected in 4.0 h. Reservoir "
            "71% full. Current release 140 m3/s. Recommended release 310 m3/s. "
            "Not spilling.\n"
            "Dam order: Begin pre-release now. Step release to 310 m3/s over the "
            "next two hours."
        ),
        "example_briefing": (
            "Inflow peaks near 820 m3/s in about four hours against 71% storage, "
            "so the buffer has to be opened up before the surge arrives. Step the "
            "release from 140 to 310 m3/s over the next two hours. The reservoir "
            "is not spilling yet, and pre-releasing now is what keeps it that way."
        ),
    },
    "disaster": {
        "label": "For the disaster officer",
        "system": (
            "You are writing the situation line for a district disaster "
            "management officer. Rewrite the facts as two or three short "
            "sentences: how long there is, who is exposed, and what to do now. "
            "Use only the numbers given, never add or change one, and never "
            "invent an instruction."
        ),
        "example_facts": (
            "Basin: Krishna Basin (Sangli).\n"
            "Flood: CRITICAL. River 1.2 m above the levee crest. Time to "
            "overtopping 85 min. Households at risk 1030. Downstream flow 870 "
            "m3/s against a levee capacity of 700 m3/s.\n"
            "Disaster order: Evacuate the low-lying riverside sectors now. Move "
            "residents to the high-ground shelters."
        ),
        "example_briefing": (
            "The river tops the levee in about 85 minutes, by roughly 1.2 m, "
            "putting 1030 households in the riverside sectors inside the flood "
            "envelope. Start moving them to the high-ground shelters now rather "
            "than on confirmation. Downstream flow is already 870 m3/s against a "
            "700 m3/s levee capacity, so the margin is gone."
        ),
    },
}


def build_messages(digest: str, role: str | None = None) -> list[dict]:
    """The exact message list sent to the endpoint for a set of facts.

    With ``role`` set, the audience-specific instruction and worked example from
    ``ROLES`` replace the combined ones — same three-turn shape, different reader.
    """
    cfg = ROLES.get(role or "")
    return [
        {"role": "system", "content": cfg["system"] if cfg else SYSTEM_PROMPT},
        {"role": "user",
         "content": cfg["example_facts"] if cfg else _EXAMPLE_FACTS},
        {"role": "assistant",
         "content": cfg["example_briefing"] if cfg else _EXAMPLE_BRIEFING},
        {"role": "user", "content": digest},
    ]


def transcript(digest: str, role: str | None = None) -> str:
    """Human-readable rendering of ``build_messages`` for the UI's audit panel."""
    return "\n\n".join(f"[{msg['role']}]\n{msg['content']}"
                       for msg in build_messages(digest, role))


def preview(state, directives, place_name: str = "") -> str:
    """Everything that would be sent for this state — nothing is hidden."""
    return transcript(build_digest(state, directives, place_name))


def _strip_markup(text: str) -> str:
    """Normalise a directive string for the model: no HTML, no odd glyphs.

    A small model is brittle about unusual tokens (``m³``, em dashes), so the
    facts are handed over as plain sentences. Only the presentation changes —
    the numbers are the engine's.
    """
    out = (str(text).replace("<b>", "").replace("</b>", "")
           .replace("&amp;", "&").replace("&nbsp;", " ")
           .replace("m³/s", "m3/s").replace("³", "3")
           .replace(" — ", ", ").replace("—", ", "))
    return " ".join(out.split()).strip()


def build_digest(state, directives, place_name: str = "") -> str:
    """The facts the briefing may use — nothing else is sent.

    Deliberately terse, and phrased as ordinary sentences: the model is
    re-wording, not reasoning, and every character here costs input tokens
    against the account budget.
    """
    def _num(value, fmt="{:.0f}", dash=""):
        try:
            if value is None:
                return dash
            fv = float(value)
            if fv != fv or fv in (float("inf"), float("-inf")):
                return dash
            return fmt.format(fv)
        except (TypeError, ValueError):
            return dash

    peak = _num(getattr(state, "inflow_peak", None))
    peak_in = _num(getattr(state, "inflow_peak_in_h", None), "{:.1f}")
    res_pct = _num(getattr(state, "reservoir_pct", None))
    release = _num(getattr(state, "firo_release", None))
    esp = _num(getattr(state, "esp", None))
    wilt = _num(getattr(state, "days_to_wilting", None))

    flood = [f"Flood: {str(getattr(state, 'flood_sev', 'unknown')).upper()}."]
    if peak:
        flood.append(f"Peak inflow {peak} m3/s expected"
                     + (f" in {peak_in} h." if peak_in else "."))
    if res_pct:
        flood.append(f"Reservoir {res_pct}% full.")
    if release:
        flood.append(f"Recommended release {release} m3/s.")

    drought = [f"Drought: {getattr(state, 'drought_sev', 'unknown')}."]
    if esp:
        drought.append(f"Evaporative stress percentile {esp}.")
    if wilt:
        drought.append(f"Days to wilting {wilt}.")

    lines = [
        f"Basin: {place_name or 'the basin'}. "
        f"Time: {getattr(state, 'clock', 'unknown')}.",
        " ".join(flood),
        " ".join(drought),
    ]
    for role, label in (("dam", "Dam order"), ("disaster", "Disaster order"),
                        ("farmer", "Farmer order")):
        item = (directives or {}).get(role) or {}
        if not item:
            continue
        first = (item.get("actions") or [""])[0]
        lines.append(_strip_markup(f"{label}: {item.get('title', '')}. {first}"))
    return "\n".join(ln for ln in lines if ln)


def _bucket(value, step: float, fmt: str = "{:.0f}", dash: str = "") -> str:
    """Format a reading rounded to ``step``.

    Rounding is not cosmetic here. The role digests are hashed to decide whether
    a situation is *new* enough to spend a call on, so an unrounded reading would
    make ordinary feed jitter (31.4 -> 31.5 mm/hr) look like news and re-bill a
    briefing every refresh. Rounding to a step the operator would not act on
    differently means one call per genuinely different situation.
    """
    try:
        if value is None:
            return dash
        fv = float(value)
        if fv != fv or fv in (float("inf"), float("-inf")):
            return dash
        return fmt.format(round(fv / step) * step)
    except (TypeError, ValueError):
        return dash


def build_role_digest(state, directives, role: str, place_name: str = "") -> str:
    """The facts one role's briefing may use — nothing else is sent.

    Narrower than ``build_digest`` on purpose: a farmer briefing that mentions
    gate schedules is noise, and every character here is an input token. Three
    deliberate choices:

    * **No wall clock.** The combined digest carries ``state.clock`` because its
      audit panel is about one moment. A role digest is hashed as a cache and
      auto-fire key, so including a clock that ticks every minute would re-bill
      three briefings a minute on a basin that had not changed. Timing is carried
      relatively instead ("in 4.0 h", "85 min"), which is what the reader needs.
    * **Bucketed numbers**, for the same reason — see ``_bucket``.
    * **The role's own order, under its usual label.** The label is what
      ``_looks_degenerate`` greps for to catch a model that echoed the facts back
      instead of re-wording them.
    """
    if role not in ROLES:
        raise KeyError(f"unknown briefing role {role!r}")

    lines = [f"Basin: {place_name or 'the basin'}."]

    if role == "farmer":
        esp = _bucket(getattr(state, "esp", None), 1)
        sm = _bucket(getattr(state, "soil_moisture", None), 0.01, "{:.2f}")
        wilt = _bucket(getattr(state, "days_to_wilting", None), 1)
        rain = _bucket(getattr(state, "rain_now", None), 0.5, "{:.1f}")
        facts = [f"Drought: {getattr(state, 'drought_sev', 'unknown')}."]
        if esp:
            facts.append(f"Evaporative stress percentile {esp}.")
        if sm:
            facts.append(f"Soil moisture {sm} m3/m3.")
        if wilt:
            facts.append(f"Days to wilting {wilt}.")
        if rain:
            facts.append(f"Rain now {rain} mm/hr.")
        lines.append(" ".join(facts))

    elif role == "dam":
        peak = _bucket(getattr(state, "inflow_peak", None), 10)
        peak_in = _bucket(getattr(state, "inflow_peak_in_h", None), 0.5, "{:.1f}")
        res_pct = _bucket(getattr(state, "reservoir_pct", None), 1)
        now_rel = _bucket(getattr(state, "release_now", None), 5)
        firo = _bucket(getattr(state, "firo_release", None), 5)
        facts = [f"Flood: {str(getattr(state, 'flood_sev', 'unknown')).upper()}."]
        if peak:
            # "expected in 0.0 h" is how a rounded zero reads, and it is not a
            # fact — at the peak or past it, say so in words the reader uses.
            if getattr(state, "past_peak", False):
                facts.append(f"Peak inflow {peak} m3/s, already passed.")
            elif peak_in and float(peak_in) > 0:
                facts.append(f"Peak inflow {peak} m3/s expected in {peak_in} h.")
            else:
                facts.append(f"Peak inflow {peak} m3/s, arriving now.")
        if res_pct:
            facts.append(f"Reservoir {res_pct}% full.")
        if now_rel:
            facts.append(f"Current release {now_rel} m3/s.")
        if firo:
            facts.append(f"Recommended release {firo} m3/s.")
        facts.append("Spilling." if getattr(state, "spilling", False)
                     else "Not spilling.")
        lines.append(" ".join(facts))

    else:                                                   # disaster
        depth = _bucket(getattr(state, "overtop_depth", None), 0.1, "{:.1f}")
        q_down = _bucket(getattr(state, "q_downstream", None), 10)
        hh = int(getattr(state, "households_at_risk", 0) or 0)
        tto_raw = getattr(state, "time_to_overtop_min", float("inf"))
        facts = [f"Flood: {str(getattr(state, 'flood_sev', 'unknown')).upper()}."]
        if depth and float(depth) > 0:
            facts.append(f"River {depth} m above the levee crest.")
        # time_to_overtop_min is inf when the river stays in channel and can round
        # to zero when the crest is already going under. Both are statements, not
        # "0 min", which would read as a missing number to the model and the eye.
        if not math.isfinite(tto_raw):
            facts.append("The river stays within the levee.")
        elif tto_raw <= 1.0:
            facts.append("The levee is overtopping now.")
        else:
            facts.append(f"Time to overtopping {_bucket(tto_raw, 5)} min.")
        facts.append(f"Households at risk {hh}.")
        if q_down:
            facts.append(f"Downstream flow {q_down} m3/s against a levee "
                         f"capacity of 700 m3/s.")
        lines.append(" ".join(facts))

    item = (directives or {}).get(role) or {}
    if item:
        label = {"farmer": "Farmer order", "dam": "Dam order",
                 "disaster": "Disaster order"}[role]
        first = (item.get("actions") or [""])[0]
        lines.append(_strip_markup(f"{label}: {item.get('title', '')}. {first}"))
    return "\n".join(ln for ln in lines if ln)


def role_preview(state, directives, role: str, place_name: str = "") -> str:
    """Everything that would be sent for one role — nothing is hidden."""
    return transcript(build_role_digest(state, directives, role, place_name),
                      role)


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _norm_num(token: str) -> str:
    """'3,000' -> '3000', '1.0' -> '1', '04' -> '4' (comparison form only)."""
    t = token.replace(",", "")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t.lstrip("0") or "0"


def unsupported_numbers(text: str, digest: str) -> list:
    """Figures in the reply that do not appear in the facts it was given.

    A deterministic guard against the one failure mode that would actually
    matter here — a language model quoting a number the engine never computed.
    It is a heuristic (a digit that happens to occur elsewhere in the facts will
    pass), so the UI reports it as a check, not a proof, and the directive cards
    stay authoritative either way.
    """
    known = {_norm_num(t) for t in _NUM_RE.findall(digest or "")}
    seen, bad = set(), []
    for token in _NUM_RE.findall(text or ""):
        norm = _norm_num(token)
        if norm in known or norm in seen:
            continue
        seen.add(norm)
        bad.append(token)
    return bad


def _looks_degenerate(text: str, digest: str,
                      strict_labels: bool = True) -> bool:
    """True when a small model has looped or echoed the facts back at us.

    Cheaper and more honest than showing an operator a wall of repeated text:
    the caller reports the briefing as unavailable and the dashboard is
    unaffected.

    ``strict_labels`` rejects a reply that still carries the fact sheet's own
    headings, which is the usual shape of an echo. The analyst turns it off: it
    is answering a question *about* the orders, so naming one is the correct
    answer rather than evidence of a loop.
    """
    body = (text or "").strip()
    if len(body) < 40:
        return True
    # echo: the model handed the fact lines straight back
    echoed = sum(1 for ln in digest.splitlines()
                 if len(ln) > 25 and ln.strip() in body)
    if echoed >= 2:
        return True
    if strict_labels and any(lbl in body for lbl in
                             ("Dam order:", "Disaster order:", "Farmer order:")):
        return True
    # repetition loop: one short phrase repeated over and over
    words = body.split()
    if len(words) >= 30 and len(set(w.lower() for w in words)) / len(words) < 0.34:
        return True
    return False


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _parse_stream(response) -> str:
    """Accumulate an SSE chat-completions stream into plain text."""
    out = []
    for raw in response.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            chunk = json.loads(line)
        except ValueError:
            continue
        for choice in chunk.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content")
            if piece is None:                       # non-streaming shape
                piece = (choice.get("message") or {}).get("content")
            if piece:
                out.append(piece)
    return "".join(out).strip()


def _extract_non_stream(payload: dict) -> str:
    for choice in payload.get("choices") or []:
        msg = (choice.get("message") or {}).get("content")
        if msg:
            return str(msg).strip()
    return ""


def _cache_id(digest: str, budget: int, role: str | None = None,
              model: str | None = None) -> str:
    """Identity of a request: same facts + same budget + same reader = same reply.

    Shared by the synchronous and asynchronous paths so a briefing fetched in the
    background is found by the caller that asked for it, and used as the auto-fire
    key so one situation can only ever cost one call.

    The model id is part of the identity, because the same facts answered by a
    different checkpoint are a different answer. ``model=None`` means the
    briefing slot, and is keyed on the constant rather than on whatever the
    catalogue resolved to: resolution happens once per process, off the render
    thread, and a key that changed the moment it landed would orphan every
    briefing already in the cache and re-bill all three roles.
    """
    mdl = model or MODEL
    return hashlib.sha256(
        f"{mdl}|{role or '-'}|{budget}|{digest}".encode("utf-8")).hexdigest()


def _cached_copy(hit: "NugenResult") -> "NugenResult":
    """A detached copy of a cached result, flagged as served from cache."""
    return NugenResult(ok=hit.ok, text=hit.text, error=hit.error, cached=True,
                       model=hit.model, max_tokens=hit.max_tokens,
                       usage=dict(hit.usage), unsupported=list(hit.unsupported))


_MODEL_404_HINT = "unable to resolve model"


def _post(messages: list[dict], key: str, budget: int,
          model: str) -> tuple[str, dict, str, int]:
    """One chat completion. Returns ``(text, usage, error, status)``.

    ``status`` is the HTTP code (0 for a transport failure), so the caller can
    tell a rejected model id — which is worth one retry against a freshly read
    catalogue — from a network blip, which is not.
    """
    payload = {
        "max_tokens": budget,
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": TEMPERATURE,
    }
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(ENDPOINT, json=payload, headers=headers,
                             stream=True, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return "", {}, f"Network error: {exc}", 0

    try:
        if resp.status_code != 200:
            detail = (resp.text or "")[:200].replace("\n", " ").strip()
            return ("", {},
                    f"HTTP {resp.status_code} from Nugen"
                    + (f": {detail}" if detail else ""),
                    resp.status_code)

        ctype = (resp.headers.get("Content-Type") or "").lower()
        usage: dict = {}
        if "text/event-stream" in ctype or "stream" in ctype:
            text = _parse_stream(resp)
        else:
            body = resp.text
            try:
                data = json.loads(body)
            except ValueError:
                # the endpoint streamed despite the header — re-parse as SSE
                text = "\n".join(
                    part[5:].strip() for part in body.splitlines()
                    if part.startswith("data:"))
                try:
                    text = "".join(
                        (json.loads(p).get("choices") or [{}])[0]
                        .get("delta", {}).get("content", "")
                        for p in text.splitlines() if p and p != "[DONE]")
                except Exception:
                    pass
                text = text.strip()
            else:
                text = _extract_non_stream(data)
                usage = data.get("usage") or {}
    except requests.RequestException as exc:
        return "", {}, f"Stream error: {exc}", 0
    finally:
        resp.close()

    return text, usage, "", 200


def complete(digest: str, api_key: str | None = None,
             max_tokens: int = DEFAULT_MAX_TOKENS,
             use_cache: bool = True, role: str | None = None,
             model: str | None = None, messages: list[dict] | None = None,
             cache_id: str | None = None,
             strict_labels: bool = True) -> NugenResult:
    """One completion request for a block of facts. Never raises.

    ``max_tokens`` is clamped to ``MAX_TOKENS_LIMIT`` (500) regardless of what
    the caller asks for, the facts are truncated to ``_PROMPT_CHAR_LIMIT``, and
    a repeat of the same facts is served from the in-process cache without
    spending a call. With ``role`` set, the audience-specific prompt is used.

    ``model`` and ``messages`` let the analyst reuse this transport — its own
    checkpoint and its own prompt shape — rather than growing a second copy of
    the budget, cache, streaming and verification logic. ``cache_id`` lets the
    background dispatcher pin the key it already handed the UI, so a result
    cannot land under a key nobody looks up.
    """
    global _calls_made

    key = (api_key or default_api_key()).strip()
    budget = max(1, min(int(max_tokens), MAX_TOKENS_LIMIT))
    digest = (digest or "")[:_PROMPT_CHAR_LIMIT]

    if not key:
        return NugenResult(
            error="No API key configured. Set NUGEN_API_KEY in the environment "
                  "(or .streamlit/secrets.toml) to enable the briefing layer.",
            max_tokens=budget)

    cid = cache_id or _cache_id(digest, budget, role, model)
    if use_cache:
        with _lock:
            hit = _cache.get(cid)
        if hit is not None:
            return _cached_copy(hit)

    # One catalogue read per process, and only on the path that is about to
    # spend a call anyway — a cache hit above must not pay for a network round
    # trip. Until it lands the default id, the exact one the API's own error
    # message names, is what gets used.
    brief_m, _ = resolve_models(key)
    mdl = model or brief_m

    with _lock:
        if _calls_made >= CALL_BUDGET:
            return NugenResult(
                error=f"Session call budget reached ({CALL_BUDGET} requests). "
                      "Restart the app to allow more.",
                max_tokens=budget, model=mdl)
        _calls_made += 1

    msgs = messages if messages is not None else build_messages(digest, role)
    text, usage, err, status = _post(msgs, key, budget, mdl)

    # A 404 naming the model means the catalogue moved under us. Re-read it and
    # try once more — but only if that produced a *different* id, so a genuinely
    # missing model cannot turn every request into two.
    if status == 404 and _MODEL_404_HINT in err.lower():
        fresh_brief, fresh_analyst = resolve_models(key, force=True)
        retry_m = fresh_analyst if model else fresh_brief
        if retry_m != mdl:
            with _lock:
                if _calls_made < CALL_BUDGET:
                    _calls_made += 1
                    retry_ok = True
                else:
                    retry_ok = False
            if retry_ok:
                mdl = retry_m
                text, usage, err, status = _post(msgs, key, budget, mdl)

    if err:
        return NugenResult(error=err, max_tokens=budget, model=mdl)

    if not text:
        return NugenResult(error="Nugen returned an empty response.",
                           max_tokens=budget, model=mdl)

    if _looks_degenerate(text, digest, strict_labels):
        result = NugenResult(
            error="The briefing model returned unusable text (it repeated the "
                  "input instead of re-wording it). Nothing on the dashboard "
                  "depends on this layer.",
            max_tokens=budget, usage=usage, model=mdl)
    else:
        result = NugenResult(ok=True, text=text, max_tokens=budget, usage=usage,
                             model=mdl,
                             unsupported=unsupported_numbers(text, digest))

    # Cache the model's verdict either way: temperature is 0.1, so an immediate
    # retry would spend quota to receive the same answer. Transport failures
    # above return early and are never cached, so they stay retryable.
    if use_cache:
        with _lock:
            _cache[cid] = result
    return result


def brief(state, directives, place_name: str = "", api_key: str | None = None,
          max_tokens: int = DEFAULT_MAX_TOKENS) -> NugenResult:
    """Convenience wrapper: digest a BasinState + directives, then complete."""
    return complete(build_digest(state, directives, place_name),
                    api_key=api_key, max_tokens=max_tokens)


def brief_role(state, directives, role: str, place_name: str = "",
               api_key: str | None = None,
               max_tokens: int = ROLE_MAX_TOKENS) -> NugenResult:
    """Blocking single-role briefing. Used by the audit panel and the tests."""
    return complete(build_role_digest(state, directives, role, place_name),
                    api_key=api_key, max_tokens=max_tokens, role=role)


# ---------------------------------------------------------------------------
# Non-blocking path
#
# A Streamlit rerun is a single synchronous pass over the script, so an inline
# requests.post with a 30 s read timeout would hold the whole console hostage —
# the page would appear frozen on exactly the updates an operator cares about,
# which is the "it looks broken until I refresh" failure this project already
# fixed once. So the call is handed to a worker thread and the result is read on
# a later rerun, which the live refresh provides for free.
#
# State lives in module-level dicts under the existing ``_lock``, NOT in
# ``st.session_state``: a worker thread cannot safely touch session state.
# ---------------------------------------------------------------------------
AUTO_CALL_BUDGET = 18       # of CALL_BUDGET; the rest stays for manual requests
MAX_INFLIGHT = 3            # three roles may fetch at once, nothing more

_inflight: set[str] = set()
_async_out: dict[str, NugenResult] = {}
_auto_calls = 0


def auto_calls_made() -> int:
    return _auto_calls


def auto_budget_left() -> int:
    return max(0, min(AUTO_CALL_BUDGET - _auto_calls, CALL_BUDGET - _calls_made))


def pending_count() -> int:
    """How many background briefings are in flight right now.

    The UI uses this to tighten its refresh interval while it is waiting, so a
    briefing appears a few seconds after the situation changed instead of on the
    next minute boundary. It falls back to the normal cadence by itself, because
    this returns to zero when the last thread lands.
    """
    with _lock:
        return len(_inflight)


def request_async(digest: str, api_key: str | None = None,
                  max_tokens: int = ROLE_MAX_TOKENS, role: str | None = None,
                  auto: bool = True, retry: bool = False) -> NugenResult | None:
    """Ask for a briefing without blocking the rerun.

    Returns a ``NugenResult`` when one is already known for these exact facts
    (from this call or an earlier one), or ``None`` while a request is in flight —
    the caller renders a "preparing" line and finds the answer on a later rerun.

    With ``auto=False`` this is a pure lookup: it reports what is already known
    and otherwise returns ``None`` without dispatching anything. That is how the
    scripted demo reads this layer, because the demo re-runs every two seconds and
    automatic fetching there would drain the session's quota in under a minute.

    Spending is bounded three ways: the result is keyed on the facts, so a
    situation that has already been briefed costs nothing however many times the
    page re-renders; ``AUTO_CALL_BUDGET`` caps automatic calls below the overall
    ``CALL_BUDGET``, leaving headroom for a deliberate request; and at most
    ``MAX_INFLIGHT`` requests exist at once.

    A transport failure is recorded rather than retried, so a dead endpoint shows
    the operator an error once instead of quietly draining the quota behind a
    permanent "preparing" line. ``retry=True`` clears that record for one attempt.
    """
    global _auto_calls

    key = (api_key or default_api_key()).strip()
    budget = max(1, min(int(max_tokens), MAX_TOKENS_LIMIT))
    digest = (digest or "")[:_PROMPT_CHAR_LIMIT]
    cid = _cache_id(digest, budget, role)

    with _lock:
        if retry:
            _async_out.pop(cid, None)
        hit = _cache.get(cid)
        if hit is not None:
            return _cached_copy(hit)
        done = _async_out.get(cid)
        if done is not None:
            return done
        if not key:
            return NugenResult(
                error="No API key configured. Set NUGEN_API_KEY in the "
                      "environment (or .streamlit/secrets.toml) to enable the "
                      "briefing layer.",
                max_tokens=budget)
        if cid in _inflight:
            return None                     # already being fetched; wait
        if not auto:
            return None                     # lookup only — never dispatch
        if _auto_calls >= AUTO_CALL_BUDGET or _calls_made >= CALL_BUDGET:
            return NugenResult(
                error=f"Automatic briefing budget reached "
                      f"({AUTO_CALL_BUDGET} requests this session). The "
                      f"directives above are unaffected.",
                max_tokens=budget)
        if len(_inflight) >= MAX_INFLIGHT:
            return None                     # queue full; picked up next rerun
        _inflight.add(cid)
        _auto_calls += 1

    def _work() -> None:
        # complete() never raises, but a thread that died silently would leave
        # this key in _inflight forever and the UI "preparing" for good.
        try:
            out = complete(digest, api_key=key, max_tokens=budget, role=role,
                           cache_id=cid)
        except Exception as exc:                            # pragma: no cover
            out = NugenResult(error=f"Briefing thread failed: {exc}",
                              max_tokens=budget)
        with _lock:
            _async_out[cid] = out
            _inflight.discard(cid)

    threading.Thread(target=_work, name=f"nugen-{role or 'brief'}",
                     daemon=True).start()
    return None


def request_role_async(state, directives, role: str, place_name: str = "",
                       api_key: str | None = None, auto: bool = True,
                       retry: bool = False) -> NugenResult | None:
    """``request_async`` for one role's facts — what the role tabs call."""
    return request_async(build_role_digest(state, directives, role, place_name),
                         api_key=api_key, max_tokens=ROLE_MAX_TOKENS, role=role,
                         auto=auto, retry=retry)


# ===========================================================================
# THE ANALYST — grounded question answering over the computed state
#
# WHY THIS IS NOT A CHATBOT BOLTED ON THE SIDE
# --------------------------------------------
# A language model asked "how long until the levee goes under?" with the whole
# console in its context will answer fluently and sometimes wrongly, and a
# wrong number in a flood warning is the worst output this project can produce.
# So the model is never the thing that reads the data. The pipeline is:
#
#   1. SCOPE     the question is matched against the topics this console
#                actually holds. No match means no call: the honest answer is
#                "this console does not hold that", and saying it costs nothing
#                and cannot be wrong.
#   2. RETRIEVE  the matched topics select fields *by name* off the state
#                object this page rendered. Ordinary attribute lookup, exact by
#                construction, and the fields it read are shown to the operator.
#   3. GROUND    a rule-built answer is composed from those fields before any
#                model runs. This is the answer of record. It is always
#                available, including with the language layer switched off.
#   4. GENERATE  the model is handed the question and the retrieved facts and
#                nothing else, and asked to say it in sentences.
#   5. VERIFY    every figure in the reply is checked back against the facts it
#                was given (``unsupported_numbers``). A reply quoting a number
#                that was never retrieved is flagged.
#   6. FALL BACK a failed, degenerate or unverified reply is discarded and the
#                grounded answer from step 3 stands.
#
# Step 3 is what makes the accuracy claim true rather than hopeful: the number
# the operator reads was computed by the engine and selected by name, and the
# model's only job is the English around it. Steps 1, 5 and 6 are the harness
# the app puts on screen — the failure modes are visible, not asserted.
# ===========================================================================
ANALYST_MAX_TOKENS = 300    # a few sentences, with room for a figure or four
ANALYST_ROLE = "analyst"
MAX_FACTS = 14              # bounds the prompt, and the operator's reading


@dataclass
class Fact:
    """One field read off the computed state, with where it came from."""
    field: str              # the attribute name — the provenance
    label: str
    value: str
    clause: str = ""        # the field as a sentence fragment, for the answer
    score: int = 0          # how many of the question's topics it covers


@dataclass
class Answer:
    """One trip through the pipeline above, with every stage's outcome kept.

    The UI renders this directly: ``grounded`` is the answer of record,
    ``result`` is the model's wording when there is one, and ``stages`` is the
    harness panel.
    """
    question: str = ""
    topics: list = field(default_factory=list)
    facts: list = field(default_factory=list)
    digest: str = ""
    grounded: str = ""
    in_scope: bool = False
    result: "NugenResult | None" = None
    stages: list = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """True when the model's wording passed every check and may be shown."""
        return bool(self.result and self.result.ok
                    and not self.result.unsupported)


# ---------------------------------------------------------------------------
# The field registry: everything the analyst may read, and when.
#
# A field reaches the model only by appearing here, so this tuple is the whole
# surface of the retrieval step — there is no path from a question to a value
# that does not pass through it.
# ---------------------------------------------------------------------------
def _n(value, fmt: str = "{:.0f}"):
    """Format a finite number, or None so the caller can drop the field."""
    try:
        if value is None or isinstance(value, bool):
            return None
        fv = float(value)
        if fv != fv or fv in (float("inf"), float("-inf")):
            return None
        return fmt.format(fv)
    except (TypeError, ValueError):
        return None


def _num(attr: str, fmt: str = "{:.0f}", unit: str = ""):
    def _get(s, d):
        txt = _n(getattr(s, attr, None), fmt)
        return None if txt is None else f"{txt} {unit}".strip()
    return _get


def _sev(attr: str):
    def _get(s, d):
        val = str(getattr(s, attr, "") or "").strip()
        return val.upper() if val else None
    return _get


def _peak_eta(s, d):
    """When the inflow peak lands — in words, because a rounded zero lies."""
    if getattr(s, "past_peak", False):
        return "already past"
    hrs = _n(getattr(s, "inflow_peak_in_h", None), "{:.1f}")
    if hrs is None:
        return None
    return "arriving now" if float(hrs) <= 0 else f"in {hrs} h"


def _tto(s, d):
    """Time to overtopping, as a clause. Infinity and zero are not numbers.

    Returned as a full clause rather than a bare figure for the same reason the
    role digests do it: "0 min" reads as a missing number, and "inf" is not a
    time. The clause is what the fact sheet carries and what the grounded
    sentence splices in, so both say the same true thing.
    """
    try:
        raw = float(getattr(s, "time_to_overtop_min", float("inf")))
    except (TypeError, ValueError):
        return None
    if raw != raw or raw == float("inf"):
        return "the river stays within the levee"
    if raw <= 1.0:
        return "the levee is overtopping now"
    if raw < 90:
        return f"the levee overtops in {raw:.0f} min"
    return f"the levee overtops in {raw / 60.0:.1f} h ({raw:.0f} min)"


def _wilting(s, d):
    try:
        raw = float(getattr(s, "days_to_wilting", float("inf")))
    except (TypeError, ValueError):
        return None
    if raw != raw or raw == float("inf"):
        return "the crop does not reach wilting point in the forecast horizon"
    return f"the crop reaches wilting point in {raw:.0f} days"


def _spilling(s, d):
    return ("the reservoir is spilling over the crest"
            if getattr(s, "spilling", False)
            else "the reservoir is not spilling")


def _households(s, d):
    """Households inside the flood envelope. Zero is "no households", not "0"."""
    try:
        val = int(getattr(s, "households_at_risk", 0) or 0)
    except (TypeError, ValueError):
        return None
    return "no households" if val <= 0 else f"{val:,} households"


def _crest(s, d):
    """Depth over the levee crest, or nothing when the river is inside it.

    A literal "0.00 m above the levee crest" is a number where a statement
    belongs, and on a safe update it reads as a measurement of an event that is
    not happening. The time-to-overtopping field already says the river stays
    within the levee, so this one simply stands down.
    """
    try:
        raw = float(getattr(s, "overtop_depth", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    return f"{raw:.2f} m" if raw > 0.005 else None


def _int(attr: str, unit: str = ""):
    def _get(s, d):
        try:
            val = int(getattr(s, attr, 0) or 0)
        except (TypeError, ValueError):
            return None
        return f"{val:,} {unit}".strip()
    return _get


# (attribute, label, getter, topics, clause template)
#
# The template is how the grounded sentence splices a field in. "{l} is {v}" is
# the default and covers most readings ("peak inflow is 852 m3/s"); a field
# whose value is already a clause carries "{v}" instead, because "time to
# levee overtopping is the river stays within the levee" is not English.
_FIELDS: tuple = (
    ("flood_sev", "Flood severity", _sev("flood_sev"),
     ("core", "flood", "levee", "people", "timing"), "{l} is {v}"),
    ("drought_sev", "Drought severity", _sev("drought_sev"),
     ("core", "drought", "crop", "soil"), "{l} is {v}"),
    ("time_to_overtop_min", "Time to levee overtopping", _tto,
     ("timing", "people", "levee", "flood"), "{v}"),
    ("households_at_risk", "Households at risk", _households,
     ("people", "levee", "flood"), "{v} are inside the flood envelope"),
    ("overtop_depth", "River above the levee crest", _crest,
     ("levee", "people", "flood"), "the river is {v} above the levee crest"),
    ("q_downstream", "Downstream flow", _num("q_downstream", "{:.0f}", "m3/s"),
     ("levee", "flood", "people"), "{l} is {v}"),
    ("rain_now", "Rainfall now", _num("rain_now", "{:.1f}", "mm/hr"),
     ("flood", "rain", "weather", "crop"), "{l} is {v}"),
    ("inflow_now", "Inflow now", _num("inflow_now", "{:.0f}", "m3/s"),
     ("flood", "inflow", "reservoir"), "{l} is {v}"),
    ("inflow_peak", "Peak inflow", _num("inflow_peak", "{:.0f}", "m3/s"),
     ("flood", "inflow", "timing", "reservoir", "release"), "{l} is {v}"),
    ("inflow_peak_in_h", "Peak inflow arrives", _peak_eta,
     ("flood", "inflow", "timing", "release"), "the inflow peak is {v}"),
    ("reservoir_pct", "Reservoir storage", _num("reservoir_pct", "{:.0f}%"),
     ("reservoir", "release", "flood"), "{l} is {v}"),
    ("reservoir_level", "Reservoir level", _num("reservoir_level", "{:.1f}", "m"),
     ("reservoir",), "{l} is {v}"),
    ("storage_aft", "Water in storage", _num("storage_aft", "{:,.0f}", "acre-ft"),
     ("reservoir",), "{l} is {v}"),
    ("buffer_now_aft", "Flood buffer available",
     _num("buffer_now_aft", "{:,.0f}", "acre-ft"), ("reservoir", "release"),
     "{l} is {v}"),
    ("release_now", "Current release", _num("release_now", "{:.0f}", "m3/s"),
     ("release", "reservoir"), "{l} is {v}"),
    ("firo_release", "Recommended release",
     _num("firo_release", "{:.0f}", "m3/s"), ("release", "reservoir"),
     "{l} is {v}"),
    ("spilling", "Spilling over the crest", _spilling,
     ("release", "reservoir"), "{v}"),
    ("esp", "Evaporative stress percentile", _num("esp", "{:.0f}"),
     ("drought", "crop", "weather", "soil"), "{l} is {v}"),
    ("soil_moisture", "Soil moisture", _num("soil_moisture", "{:.2f}", "m3/m3"),
     ("soil", "crop", "drought"), "{l} is {v}"),
    ("days_to_wilting", "Crop reaches wilting point", _wilting,
     ("crop", "drought", "timing", "soil"), "{v}"),
    ("temp", "Air temperature", _num("temp", "{:.1f}", "C"), ("weather",),
     "{l} is {v}"),
    ("pet", "Potential evapotranspiration", _num("pet", "{:.1f}", "mm/day"),
     ("weather", "drought"), "{l} is {v}"),
    ("et_actual", "Actual evapotranspiration",
     _num("et_actual", "{:.1f}", "mm/day"), ("weather", "drought"),
     "{l} is {v}"),
    ("real_compute_ms", "Engine execution time this update",
     _num("real_compute_ms", "{:.2f}", "ms"), ("engine",),
     "the engine computed this update in {v}"),
    ("kge", "KGE validation score", _num("kge", "{:.2f}"), ("accuracy",),
     "the {l} is {v}"),
    ("pod", "Drought detection probability", _num("pod", "{:.2f}"),
     ("accuracy",), "the {l} is {v}"),
    ("r_smap", "Agreement with NASA SMAP", _num("r_smap", "{:.2f}"),
     ("accuracy",), "{l} is {v}"),
    ("lead_time_days", "Drought warning lead time",
     _int("lead_time_days", "days"), ("accuracy", "crop", "timing"),
     "{l} is {v}"),
)

# The standing orders are facts too: the analyst must answer "what should the
# duty engineer do now?" with the directive the engine issued, not with advice
# of its own.
_ORDERS: tuple = (
    ("dam", "Order for the duty engineer",
     ("release", "reservoir", "action_dam")),
    ("disaster", "Order for the disaster officer",
     ("people", "levee", "action_disaster")),
    ("farmer", "Order for the farmer",
     ("crop", "soil", "drought", "action_farmer")),
)

# Question words -> topic.
#
# Matching is on word boundaries, not bare substrings. That is not fussiness:
# "gate" as a substring matches "irri-GATE", which quietly routed every farmer
# question through the reservoir-release fields. A trailing ``*`` marks a stem
# and matches any continuation ("irrigat*" covers irrigate, irrigation,
# irrigating); everything else must match a whole word.
_TOPIC_WORDS: dict = {
    "flood": ("flood*", "river", "rivers", "surge", "inundat*", "overflow*",
              "danger*", "severity", "alert", "alerts", "emergency",
              "situation", "how bad", "status", "risk", "risks"),
    "timing": ("how long", "when", "time to", "minute*", "hour*", "soon",
               "until", "left", "eta", "arrive*", "arriving", "before",
               "deadline"),
    "people": ("household*", "people", "person*", "resident*", "village*",
               "evacuat*", "shelter*", "affected", "population", "public",
               "who is", "civilian*"),
    "reservoir": ("reservoir*", "dam", "dams", "storage", "stored",
                  "water level", "how full", "capacity", "buffer"),
    "release": ("release*", "releasing", "gate", "gates", "discharge*",
                "outflow", "spill*", "firo", "pre-release", "prerelease",
                "let out", "draw down", "drawdown"),
    "inflow": ("inflow*", "incoming", "upstream", "peak*", "flow rate"),
    "rain": ("rain*", "precipitation", "shower*", "monsoon", "storm*",
             "downpour"),
    "levee": ("levee*", "embankment*", "crest", "downstream", "channel",
              "overtop*", "breach*", "bund"),
    "drought": ("drought*", "dry", "dryness", "stress*", "scarcity", "esp",
                "evaporative"),
    "soil": ("soil", "moistur*", "root zone", "groundwater"),
    "crop": ("crop*", "wilt*", "harvest*", "farm", "farms", "farming",
             "irrigat*", "sow*", "field*", "plant*", "agricultur*", "yield*",
             "kharif", "rabi"),
    "weather": ("temperature", "temp", "how hot", "heat", "evaporation",
                "weather", "humid*"),
    "engine": ("comput*", "how fast", "speed", "runtime", "millisecond*",
               "performance", "latency"),
    "accuracy": ("accurac*", "accurate", "kge", "valid*", "reliab*", "trust*",
                 "confiden*", "smap", "proven", "benchmark*", "how good"),
    "action_dam": ("duty engineer", "engineer*", "control room", "operator*",
                   "gate team"),
    "action_disaster": ("disaster*", "ndrf", "rescue", "response team",
                        "district officer"),
    "action_farmer": ("farmer*", "grower*", "cultivator*"),
}

# Naming a desk implies that desk's data: "what should the duty engineer do?"
# is a question about release and storage, not only about the standing order.
_DESK_TOPICS: dict = {
    "action_dam": ("release", "reservoir"),
    "action_disaster": ("people", "levee"),
    "action_farmer": ("crop", "soil", "drought"),
}

# Phrasings that make a question about what to DO rather than what IS. When one
# fires and no single desk was named, all three standing orders are retrieved --
# "what do we do now" is a question about every desk.
_ACTION_WORDS = ("what should", "what do i", "what do we", "should i",
                 "should we", "what action", "recommend", "advice", "advise",
                 "next step", "do now", "what now", "instruction", "order",
                 "tell me what", "guidance")


def _patterns(words) -> list:
    """Word-boundary patterns for a topic's keywords. See ``_TOPIC_WORDS``."""
    out = []
    for word in words:
        stem = word.endswith("*")
        body = re.escape(word[:-1] if stem else word)
        out.append(re.compile(r"\b" + body + ("" if stem else r"\b")))
    return out


_TOPIC_RE: dict = {topic: _patterns(words)
                   for topic, words in _TOPIC_WORDS.items()}
_ACTION_RE: list = _patterns(_ACTION_WORDS)

SUGGESTED_QUESTIONS = (
    "How long until the levee overtops, and how many households are exposed?",
    "What should the duty engineer do with the gates right now?",
    "Can the reservoir absorb the peak, or do we need to pre-release?",
    "Should farmers irrigate this week?",
    "How bad is the flood situation, in plain terms?",
    "How fast does the engine compute this, and how accurate is it?",
)


def _topics_for(question: str) -> list:
    """Topics this question is asking about. Never raises.

    A named desk pulls in its own domain topics too (see ``_DESK_TOPICS``), so
    the answer carries the readings behind the order and not only the order.
    """
    q = " ".join(str(question or "").lower().split())
    found = [topic for topic, pats in _TOPIC_RE.items()
             if any(p.search(q) for p in pats)]
    for desk, implied in _DESK_TOPICS.items():
        if desk in found:
            found += [t for t in implied if t not in found]
    if any(p.search(q) for p in _ACTION_RE):
        found.append("action")
    return found


def _lead(label: str) -> str:
    """A label for mid-sentence use: lower-cased unless it opens on an acronym."""
    head = label.split(" ")[0]
    return label if head.isupper() else label[0].lower() + label[1:]


def retrieve(state, directives, question: str) -> tuple:
    """``(facts, topics)`` -- the fields this question selects, read by name.

    The step the model is kept out of. Everything returned came off the state
    object by attribute lookup, so the figures are the engine's own and the UI
    can show the operator exactly which ones were read.

    Facts are RANKED by how many of the question's topics each one covers, and
    only then truncated to ``MAX_FACTS``. Ranking is not cosmetic: asked "how
    long until the levee overtops", registry order put flood severity and peak
    inflow first and pushed time-to-overtopping off the end of the sheet -- the
    one field the question was about. Score first, cut second.
    """
    topics = _topics_for(question)
    asked = {t for t in topics if t != "action"}
    wanted = asked | {"core"}
    action = "action" in topics

    facts: list = []
    for attr, label, getter, field_topics, template in _FIELDS:
        if not wanted.intersection(field_topics):
            continue
        try:
            value = getter(state, directives)
        except Exception:                                   # pragma: no cover
            value = None
        if not value:
            continue
        facts.append(Fact(
            field=attr, label=label, value=str(value),
            clause=template.format(l=_lead(label), v=value),
            score=len(asked.intersection(field_topics))))

    named_desk = any(t.startswith("action_") for t in topics)
    for role, label, order_topics in _ORDERS:
        hit = asked.intersection(order_topics)
        if not hit and not (action and not named_desk):
            continue
        item = (directives or {}).get(role) or {}
        if not item:
            continue
        first = (item.get("actions") or [""])[0]
        text = _strip_markup(f"{item.get('title', '')}. {first}").strip(". ")
        if not text:
            continue
        # An order IS the answer to an action question, so it outranks any
        # single reading whenever one was asked for.
        facts.append(Fact(field=f"directives[{role}]", label=label,
                          value=text + ".", clause="",
                          score=len(hit) + (3 if action else 0)))

    facts.sort(key=lambda f: -f.score)
    return facts[:MAX_FACTS], topics


def _grounded_answer(facts: list) -> str:
    """The answer of record, composed from the retrieved fields by rule.

    No model has run at this point and none has to: every figure here was read
    off the state by name. This is what the console shows as the answer, and
    what stands if the language layer is off, over budget, or caught quoting a
    number the engine never computed.

    Four clauses, not fourteen. ``retrieve`` has already put the fields the
    question was actually about at the front, and the evidence table beside this
    carries the rest -- a sentence reciting every retrieved reading answers
    nothing.
    """
    if not facts:
        return ""
    clauses = [f.clause for f in facts if f.clause][:4]
    orders = [f for f in facts if f.field.startswith("directives[")][:3]
    out = ""
    if len(clauses) > 1:
        out = "Right now, " + "; ".join(clauses) + "."
    elif clauses:
        out = clauses[0][0].upper() + clauses[0][1:] + "."
    for f in orders:
        out += f" {f.label}: {f.value}"
    return out.strip()


ANALYST_SYSTEM = (
    "You are the duty analyst in a river-basin flood and drought control room. "
    "Answer the operator's question in two or three short sentences, using ONLY "
    "the FACTS given. Quote the exact numbers from the FACTS with their units. "
    "Never add, estimate, convert or change a number, and never invent an "
    "instruction. If the FACTS do not answer the question, say that this "
    "console does not hold that figure. Do not greet, do not repeat the "
    "question back, and do not list the facts."
)

_ANALYST_EXAMPLE_Q = (
    "QUESTION: How long before the levee goes under, and who is exposed?\n"
    "FACTS:\n"
    "Basin: Krishna Basin (Sangli). Time: 02 Aug 2026, 09:00 IST.\n"
    "Flood severity: WARNING\n"
    "Time to levee overtopping: 85 min\n"
    "River above the levee crest: 1.20 m\n"
    "Households at risk: 1,030 households\n"
    "Downstream flow: 870 m3/s\n"
    "Order for the disaster officer: Evacuate the low-lying riverside sectors "
    "now. Move residents to the high-ground shelters."
)
_ANALYST_EXAMPLE_A = (
    "The levee is about 85 minutes from overtopping, with the river running "
    "1.20 m above the crest and downstream flow at 870 m3/s. That puts 1,030 "
    "households inside the flood envelope. The standing order is to evacuate "
    "the low-lying riverside sectors now and move residents to the high-ground "
    "shelters."
)


def build_analyst_digest(facts: list, question: str, place_name: str = "",
                         clock: str = "") -> str:
    """The question and the retrieved facts — the entire prompt payload.

    This is also the text ``unsupported_numbers`` verifies the reply against, so
    the prompt and the guard read the same string and cannot disagree about what
    the model was told.
    """
    head = f"Basin: {place_name or 'the basin'}."
    if clock:
        head += f" Time: {clock}."
    lines = [f"QUESTION: {' '.join(str(question or '').split())}", "FACTS:",
             head]
    lines += [f"{f.label}: {f.value}" for f in facts]
    return "\n".join(lines)


def build_analyst_messages(digest: str) -> list:
    """The same proven three-turn shape as the briefings: rule, example, task."""
    return [
        {"role": "system", "content": ANALYST_SYSTEM},
        {"role": "user", "content": _ANALYST_EXAMPLE_Q},
        {"role": "assistant", "content": _ANALYST_EXAMPLE_A},
        {"role": "user", "content": digest},
    ]


def analyst_preview(state, directives, question: str,
                    place_name: str = "") -> str:
    """Everything that would be sent for this question — nothing is hidden."""
    facts, _ = retrieve(state, directives, question)
    digest = build_analyst_digest(facts, question, place_name,
                                  getattr(state, "clock", ""))
    return "\n\n".join(f"[{msg['role']}]\n{msg['content']}"
                       for msg in build_analyst_messages(digest))


def ask(state, directives, question: str, place_name: str = "",
        api_key: str | None = None, use_model: bool = True) -> Answer:
    """Run one question through the whole pipeline. Never raises.

    The returned :class:`Answer` has a ``grounded`` field that is correct
    whatever happened to the model, and a ``stages`` list recording what each
    step of the harness did — which is what the tab puts on screen.
    """
    question = " ".join(str(question or "").split())
    facts, topics = retrieve(state, directives, question)
    out = Answer(question=question, topics=topics, facts=facts)

    # ---- 1. scope -------------------------------------------------------
    readings = [f for f in facts if not f.field.startswith("directives[")]
    out.in_scope = bool(topics) and bool(facts)
    if not out.in_scope:
        out.stages = [
            ("Scope check", "out of scope",
             "The question did not match any field this console computes, so "
             "nothing was retrieved and no model was called."),
            ("Retrieval", "0 fields", "There was nothing to read."),
            ("Grounded answer", "declined",
             "Answering outside the computed state would be a guess, so the "
             "console says so instead."),
            ("Language model", "not called", "No tokens spent."),
            ("Figure check", "not needed", "There is no reply to check."),
            ("Fallback", "not applicable", "No answer was produced."),
        ]
        return out

    # ---- 2. retrieve + 3. ground ----------------------------------------
    out.grounded = _grounded_answer(facts)
    out.digest = build_analyst_digest(facts, question, place_name,
                                      getattr(state, "clock", ""))
    n_orders = len(facts) - len(readings)
    stages = [
        ("Scope check", "in scope",
         f"Matched {len(topics)} topic{'' if len(topics) == 1 else 's'} "
         f"({', '.join(topics[:6])}) against the {len(_FIELDS)} fields and "
         f"{len(_ORDERS)} standing orders this console holds."),
        ("Retrieval", f"{len(facts)} field{'' if len(facts) == 1 else 's'}",
         f"{len(readings)} reading{'' if len(readings) == 1 else 's'} and "
         f"{n_orders} standing order{'' if n_orders == 1 else 's'}, read by "
         f"name off the state object this page rendered. Every figure is the "
         f"engine's own."),
        ("Grounded answer", "computed",
         "Composed from those fields by rule, before any model ran. This is "
         "the answer of record and it does not depend on the language layer."),
    ]

    # ---- 4. generate ----------------------------------------------------
    if not use_model:
        out.stages = stages + [
            ("Language model", "not called",
             "The grounded answer was requested on its own, so no tokens were "
             "spent."),
            ("Figure check", "not needed", "There is no reply to check."),
            ("Fallback", "not needed",
             "The grounded answer stands, as it does in every other case."),
        ]
        return out

    # Resolve rather than read. ``known_models`` returns the unresolved defaults
    # until ``complete`` has read the catalogue once, so the FIRST question of a
    # process would be answered by the briefing checkpoint — the smallest one —
    # which is precisely the question a reviewer asks first. This path is
    # already blocking under a spinner and about to spend a 30-second POST, so
    # one catalogue read here costs nothing anybody notices. With no key it
    # returns the defaults without touching the network.
    _, analyst_m = resolve_models(api_key)
    res = complete(out.digest, api_key=api_key, max_tokens=ANALYST_MAX_TOKENS,
                   role=ANALYST_ROLE,
                   model=analyst_m if analyst_m != MODEL else None,
                   messages=build_analyst_messages(out.digest),
                   strict_labels=False)
    out.result = res

    if not res.ok:
        out.stages = stages + [
            ("Language model", "unavailable", res.error),
            ("Figure check", "not reached", "There is no reply to check."),
            ("Fallback", "in use",
             "The grounded answer above is what the console is showing — which "
             "is the point of computing it first."),
        ]
        return out

    spend = ("served from cache, no tokens spent" if res.cached
             else (f"{res.tokens_out} completion tokens"
                   if res.tokens_out else f"{res.max_tokens}-token cap"))
    stages.append(("Language model", res.model or MODEL,
                   f"{spend}. It was handed the question and the "
                   f"{len(facts)} retrieved facts, and nothing else."))

    # ---- 5. verify + 6. fall back ---------------------------------------
    if res.unsupported:
        stages += [
            ("Figure check", "failed",
             f"{', '.join(str(b) for b in res.unsupported[:6])} does not appear "
             f"in the retrieved facts, so the wording is withheld."),
            ("Fallback", "in use",
             "The grounded answer above is shown instead. A figure the engine "
             "never computed does not reach the operator."),
        ]
    else:
        stages += [
            ("Figure check", "passed",
             "Every number in the reply appears in the retrieved facts, so the "
             "wording quotes the engine rather than the model."),
            ("Fallback", "not needed",
             "The grounded answer is still shown beneath it, unchanged."),
        ]
    out.stages = stages
    return out
