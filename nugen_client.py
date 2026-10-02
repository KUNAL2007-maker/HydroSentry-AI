"""
nugen_client.py — the plain-language briefing layer (Nugen inference API)
==============================================================================

WHAT THIS IS
------------
A thin, defensive client for the Nugen chat-completions endpoint. It takes the
directives the deterministic rule engine has *already* produced and asks a small
hosted model (``qwen-v2p5-0p5b-instruct``) to re-word them for one audience at a
time: the farmer, the dam duty engineer, and the district disaster officer each
get the same decision in the register they actually read.

This is a first-class layer of the product, not a demo toggle. It is wired into
all three operator tabs and refreshes itself as the basin changes.

WHAT THIS IS NOT
----------------
It is **not** part of the forecast. It never computes, predicts or changes a
number — every figure it is allowed to mention is handed to it in the prompt,
and if the call fails, is not configured, or is out of budget, the console is
unaffected: the rule-based directives are the product, and the directive cards
stay on screen either way. That separation is the point, not a limitation: a
0.5B model is a good writer and a bad hydrologist, so it is given the writing
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
MODEL = "qwen-v2p5-0p5b-instruct"

MAX_TOKENS_LIMIT = 500      # hard account cap — never exceeded
DEFAULT_MAX_TOKENS = 220    # a briefing is short; stay well inside the cap
TEMPERATURE = 0.1           # near-deterministic: this is a re-wording task
TIMEOUT = (5, 30)           # (connect, read) seconds
CALL_BUDGET = 24            # max network calls per process (quota guard)
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
# Shape matters more than wording on a 0.5B model. Three shapes were tried live
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
# same three-turn shape proven above. The example is what keeps a 0.5B model from
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

    A 0.5B model is brittle about unusual tokens (``m³``, em dashes), so the
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


def _looks_degenerate(text: str, digest: str) -> bool:
    """True when a small model has looped or echoed the facts back at us.

    Cheaper and more honest than showing an operator a wall of repeated text:
    the caller reports the briefing as unavailable and the dashboard is
    unaffected.
    """
    body = (text or "").strip()
    if len(body) < 40:
        return True
    # echo: the model handed the fact lines straight back
    echoed = sum(1 for ln in digest.splitlines()
                 if len(ln) > 25 and ln.strip() in body)
    if echoed >= 2 or any(lbl in body for lbl in
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


def _cache_id(digest: str, budget: int, role: str | None = None) -> str:
    """Identity of a request: same facts + same budget + same reader = same reply.

    Shared by the synchronous and asynchronous paths so a briefing fetched in the
    background is found by the caller that asked for it, and used as the auto-fire
    key so one situation can only ever cost one call.
    """
    return hashlib.sha256(
        f"{MODEL}|{role or '-'}|{budget}|{digest}".encode("utf-8")).hexdigest()


def _cached_copy(hit: "NugenResult") -> "NugenResult":
    """A detached copy of a cached result, flagged as served from cache."""
    return NugenResult(ok=hit.ok, text=hit.text, error=hit.error, cached=True,
                       model=hit.model, max_tokens=hit.max_tokens,
                       usage=dict(hit.usage), unsupported=list(hit.unsupported))


def complete(digest: str, api_key: str | None = None,
             max_tokens: int = DEFAULT_MAX_TOKENS,
             use_cache: bool = True, role: str | None = None) -> NugenResult:
    """One briefing request for a block of facts. Never raises.

    ``max_tokens`` is clamped to ``MAX_TOKENS_LIMIT`` (500) regardless of what
    the caller asks for, the facts are truncated to ``_PROMPT_CHAR_LIMIT``, and
    a repeat of the same facts is served from the in-process cache without
    spending a call. With ``role`` set, the audience-specific prompt is used.
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

    cache_id = _cache_id(digest, budget, role)
    if use_cache:
        with _lock:
            hit = _cache.get(cache_id)
        if hit is not None:
            return _cached_copy(hit)

    with _lock:
        if _calls_made >= CALL_BUDGET:
            return NugenResult(
                error=f"Session call budget reached ({CALL_BUDGET} requests). "
                      "Restart the app to allow more.",
                max_tokens=budget)
        _calls_made += 1

    payload = {
        "max_tokens": budget,
        "model": MODEL,
        "messages": build_messages(digest, role),
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
        return NugenResult(error=f"Network error: {exc}", max_tokens=budget)

    try:
        if resp.status_code != 200:
            detail = (resp.text or "")[:200].replace("\n", " ").strip()
            return NugenResult(
                error=f"HTTP {resp.status_code} from Nugen"
                      + (f": {detail}" if detail else ""),
                max_tokens=budget)

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
        return NugenResult(error=f"Stream error: {exc}", max_tokens=budget)
    finally:
        resp.close()

    if not text:
        return NugenResult(error="Nugen returned an empty response.",
                           max_tokens=budget)

    if _looks_degenerate(text, digest):
        result = NugenResult(
            error="The briefing model returned unusable text (it repeated the "
                  "input instead of re-wording it). Nothing on the dashboard "
                  "depends on this layer.",
            max_tokens=budget, usage=usage)
    else:
        result = NugenResult(ok=True, text=text, max_tokens=budget, usage=usage,
                             unsupported=unsupported_numbers(text, digest))

    # Cache the model's verdict either way: temperature is 0.1, so an immediate
    # retry would spend quota to receive the same answer. Transport failures
    # above return early and are never cached, so they stay retryable.
    if use_cache:
        with _lock:
            _cache[cache_id] = result
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
            out = complete(digest, api_key=key, max_tokens=budget, role=role)
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
