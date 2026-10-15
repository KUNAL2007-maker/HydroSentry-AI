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
  smaller again for a single question.
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
    # Fragments where the reply stated or forecast a hazard, which is this
    # layer's one forbidden act. Non-empty means the wording is discarded.
    hazards: list = field(default_factory=list)

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

# Held across the whole catalogue read, so exactly one thread fetches and the
# others wait and then see the resolved answer. _lock is still what guards
# the globals themselves; this one only serialises the network round trip.
_resolve_lock = threading.Lock()
_models_resolved = False
_briefing_model = MODEL
_analyst_model = MODEL
_catalogue_note = "not read yet"

# "0.49B", "7B", "560M", and the mixture-of-experts "8x7B" (= 56 B, which
# read as 8 B and slipped under the analyst ceiling). The B/M suffix is
# mandatory: a bare number is a size we cannot interpret, and guessing one
# is how an unknown model gets promoted into the slot where latency matters.
_SIZE_RE = re.compile(r"(?:([0-9]+)\s*x\s*)?([0-9]*\.?[0-9]+)\s*([BM])", re.I)


def _params_b(entry: dict) -> float:
    """Parameter count in billions from a catalogue entry's '0.49B' / '7B'.

    An unparseable size returns ``inf``, which sorts last when picking the
    smallest and is excluded by the analyst's ceiling — an unknown model is
    never silently promoted into the slot where latency matters.
    """
    mt = _SIZE_RE.search(str(entry.get("parameters") or "").strip())
    if not mt:
        return float("inf")
    val = float(mt.group(2))
    if mt.group(3).upper() == "M":
        val /= 1000.0
    return val * float(mt.group(1) or 1)


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
    global _catalogue_note

    with _lock:
        if _models_resolved and not force:
            return _briefing_model, _analyst_model

    with _resolve_lock:
        # Re-check inside the gate: while this thread queued, another may
        # have done the work, and a second listing would answer the same.
        with _lock:
            if _models_resolved and not force:
                return _briefing_model, _analyst_model
        return _read_catalogue(api_key)


def _read_catalogue(api_key: str | None) -> tuple[str, str]:
    """The body of ``resolve_models``, run under ``_resolve_lock``."""
    global _models_resolved, _briefing_model, _analyst_model, _catalogue_note

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
                body = resp.json()
                listed = body.get("models") if isinstance(body, dict) else None
                rows = [e for e in (listed or [])
                        if isinstance(e, dict) and _usable(e)]
                note = (f"{len(rows)} inference-ready text model"
                        f"{'' if len(rows) == 1 else 's'} on this account"
                        if rows else
                        f"the catalogue listed no inference-ready text model — "
                        f"falling back to {MODEL}")
        except Exception as exc:        # never raise onto the render thread
            note = (f"could not read the catalogue ({type(exc).__name__})"
                    f" — falling back to {MODEL}")

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
        # No candidate under the ceiling: take the SMALLEST, never the
        # largest. Falling back upwards would hand the analyst exactly the
        # model the ceiling was written to keep out of a 30 s read timeout.
        analyst_m = str((fits or by_size[:1])[-1]["model_id"])
        if not fits:
            note += (f" (none at or under {_ANALYST_MAX_B:.0f}B, so the "
                     f"analyst uses the smallest too)")
    elif rows:
        note += " (none instruction-tuned)"

    with _lock:
        _catalogue_note = note
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
# The hazard guard
#
# The deterministic backstop for the rule this layer exists under: the engine
# states hazards, and the assistance layer does not. A reply that forecasts
# anyway is discarded and the computed answer stands.
#
# Two design decisions carry the whole thing, and both are there to protect the
# FALSE POSITIVE case, because a guard that fires on correct output is worse
# than no guard at all — it would censor the console's own engine:
#
#   * A compound-noun shield. The bare word "flood" has to pass, because
#     "move to the flood shelter", "the flood envelope" and "the flood-affected
#     sectors" are exactly the safe-place wording this layer is FOR. Only
#     "flood" in a predictive construction is caught.
#   * Forward clock only. "in the last 3 hours" passes: reporting what already
#     happened is the alert job. Only "in 3 hours" is a forecast.
#
# And one exemption: ``allow`` takes the fact sheet the model was handed, so a
# fragment occurring verbatim in the facts is a quotation of the engine rather
# than a claim of the model's own. That is the same reasoning
# ``unsupported_numbers`` already applies to figures.
#
# WARNING — only ever call this on MODEL OUTPUT. The engine's own correct
# prose legitimately trips it ("the levee overtops in 85 min" is a true
# statement the engine is entitled to make), so it must never be applied to
# ``Answer.grounded``, to a digest, to a directive, or to a Pattern's detail.
# ---------------------------------------------------------------------------
_HZ_TAIL = (r"(?:shelter|relief|camp|cent(?:re|er)|envelope|zone|gate|plain"
            r"|wall|warning|watch|severity|level|line|sector|map|kit|depot"
            r"|protocol|pool|season|control|response|team|cell|desk|bund"
            r"|marker|mark|post|advisory|bulletin|siren|alarm|barrier"
            r"|defence|defense|insurance|routine|drill|irrigation|tanker"
            r"|convoy|bus|boat|helpline|register|list|report)s?\b")
_HZ_BRIDGE = r"(?:[-\s]+(?:affected|hit|prone|free|related|resilient|stricken))?"
_HZ_NOT_COMPOUND = r"(?!%s[-\s]+%s)" % (_HZ_BRIDGE, _HZ_TAIL)

_HZ_HAZARD = (
    r"(?:flood(?:s|ing|ed)?%(nc)s|drought%(nc)s"
    r"|inundat(?:e|es|ed|ing|ion)|overtop(?:s|ped|ping)?"
    r"|breach(?:es|ed|ing)?|submerg(?:e|es|ed|ing)|spill(?:s|ed|ing)?(?!way)"
    r"|under\s*water|go(?:es|ing)?\s+under|dry(?:ing)?\s+out"
    r"|dr(?:ies|y)\s+up|wilt(?:s|ed|ing)?|crop\s+failure|cloudburst"
    r"|surge(?:s|d|ing)?|crest(?:s|ed|ing)?|peak(?:s|ed|ing)?"
    r"|ris(?:e|es|en|ing)|swell(?:s|ing)?|exceed(?:s|ed|ing)?"
    r"|top(?:s|ped|ping)?\s+the\s+(?:levee|bund|crest|embankment)"
    r"|danger\s+(?:mark|level))" % {"nc": _HZ_NOT_COMPOUND})

# "should" and "must" are deliberately absent: the entire mitigation register is
# built on them, and catching them would censor every measure this layer exists
# to deliver.
_HZ_MODAL = (r"(?:will|'ll|shall|won't|going\s+to|about\s+to|set\s+to"
             r"|due\s+to|on\s+(?:track|course)\s+to|poised\s+to"
             r"|headed\s+for|heading\s+for|bound\s+for"
             r"|expect(?:s|ed|ing)?|anticipat(?:e|es|ed|ing)"
             r"|forecast(?:s|ed|ing)?|predict(?:s|ed|ing)?"
             r"|project(?:s|ed|ing)?|likel(?:y|ihood)|probabl[ey]"
             r"|may|might|could|imminent|impending|looming)")

_HZ_NUM = (r"(?:\d[\d,.]*|a|an|one|two|three|four|five|six|seven|eight|nine"
           r"|ten|twelve)")
_HZ_UNIT = r"(?:second|sec|minute|min|hour|hr|day|week)s?\b"
_HZ_HEDGE = r"(?:about|around|roughly|approximately|some|just|under|over|nearly|almost)"
# Forward-looking only: the negative lookahead is what lets a retrospective
# window ("in the last 3 hours") through, which is the alert job.
_HZ_SOON = (r"(?:(?:in|within|inside)\s+(?!the\s+(?:last|past|previous|preceding))"
            r"(?:%s\s+)?%s\s*%s"
            r"|by\s+(?:tomorrow|tonight|midnight|dawn|daybreak|nightfall|morning)"
            r"|before\s+(?:dawn|midnight|nightfall|morning))"
            % (_HZ_HEDGE, _HZ_NUM, _HZ_UNIT))

_HZ_TREND = (r"(?:rising|climbing|mounting|swelling|surging|building|drying"
             r"|falling|dropping|worsening|deteriorating|approaching|nearing"
             r"|creeping|edging|trending|depleting|outpacing|outstripping"
             r"|heading|headed|bound|tracking)")
_HZ_SUBJ = (r"(?:river|water|waters|level|levels|stage|inflow|flow|discharge"
            r"|soil|moisture|root\s*zone|crop|crops|field|fields|ground"
            r"|storage|reservoir|crest|basin|situation|conditions?)")
_HZ_SOFT_V = (r"(?:overtop\w*|breach\w*|crest\w*|inundat\w*|submerg\w*"
              r"|peak\w*|wilt\w*|dry\s+out|go\s+under)")

W = 40      # proximity window, characters, applied inside one sentence

_HZ_RULES = tuple(re.compile(p, re.I) for p in (
    r"%s.{0,%d}?%s" % (_HZ_MODAL, W, _HZ_HAZARD),
    r"%s.{0,%d}?%s" % (_HZ_HAZARD, W, _HZ_MODAL),
    r"%s.{0,%d}?%s" % (_HZ_HAZARD, W, _HZ_SOON),
    r"%s.{0,%d}?%s" % (_HZ_SOON, W, _HZ_HAZARD),
    r"(?:risk|threat|chance|danger|likelihood|probability|odds)\s+of"
    r".{0,%d}?%s" % (W, _HZ_HAZARD),
    r"(?:at\s+th(?:is|ese)\s+rate|on\s+current\s+trends?"
    r"|if\s+(?:this|it)\s+continues|a\s+matter\s+of\s+time)",
    r"%s\s+(?:is|are)\s+(?:\w+ly\s+)?%s" % (_HZ_SUBJ, _HZ_TREND),
    r"%s\s+(?:towards?|past|beyond|above|up\s+to|out)\b" % _HZ_TREND,
    r"faster\s+than\s+(?:the\s+)?(?:crop|soil|root|plant|field)",
    r"%s\s+(?:arrive\w*|hits?|lands?|comes?)" % _HZ_HAZARD,
    r"before\s+(?:\w+\s+){0,2}?%s" % _HZ_HAZARD,
    r"(?:time|minutes|hours|days|eta)\s+to\s+(?:the\s+)?(?:levee\s+)?%s"
    % _HZ_HAZARD,
    r"%s\s*%s\s+(?:away\s+)?(?:from|ahead\s+of|shy\s+of)"
    r"\s+(?:\w+\s+){0,2}?%s" % (_HZ_NUM, _HZ_UNIT, _HZ_HAZARD),
    r"(?:should|must|has\s+to|have\s+to)\s+(?:\w+\s+){0,2}?%s" % _HZ_SOFT_V,
))


def states_a_hazard(text: str, allow: str = "") -> list:
    """Fragments where the reply states or forecasts a hazard. Never raises.

    ``allow`` is the fact sheet the model was given; a fragment appearing
    verbatim there is the engine's own statement being quoted back and is not
    counted. See this section's header for the false-positive reasoning, and for
    why this must never be run over the engine's own prose.
    """
    body = str(text or "")
    facts = str(allow or "").lower()
    found, seen = [], set()
    for sentence in re.split(r"[.!?;:\n]", body):
        if len(sentence.strip()) < 4:
            continue
        for rule in _HZ_RULES:
            for mt in rule.finditer(sentence):
                frag = " ".join(mt.group(0).split())
                low = frag.lower()
                if low in seen or (facts and low in facts):
                    continue
                seen.add(low)
                found.append(frag)
    return found


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
                       usage=dict(hit.usage), unsupported=list(hit.unsupported),
                       hazards=list(hit.hazards))


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
             cache_id: str | None = None, strict_labels: bool = True,
             auto: bool = False) -> NugenResult:
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
    global _calls_made, _auto_calls

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
    brief_m, analyst_m = resolve_models(key)
    mdl = model or (analyst_m if role == ANALYST_ROLE else brief_m)

    with _lock:
        if _calls_made >= CALL_BUDGET:
            return NugenResult(
                error=f"Session call budget reached ({CALL_BUDGET} requests). "
                      "Restart the app to allow more.",
                max_tokens=budget, model=mdl)
        _calls_made += 1

    msgs = (messages if messages is not None
            else build_analyst_messages(digest))
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
                    # A retry on an automatic briefing is a second real
                    # call and has to be charged to the same allowance, or
                    # an endpoint that 404s doubles what the automatic path
                    # is permitted to spend.
                    if auto:
                        _auto_calls += 1
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
                             unsupported=unsupported_numbers(text, digest),
                             hazards=states_a_hazard(text, digest))

    # Cache the model's verdict either way: temperature is 0.1, so an immediate
    # retry would spend quota to receive the same answer. Transport failures
    # above return early and are never cached, so they stay retryable.
    if use_cache:
        with _lock:
            _cache[cid] = result
    return result


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
                  max_tokens: int = DEFAULT_MAX_TOKENS, role: str | None = None,
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
                           cache_id=cid, auto=True)
        except Exception as exc:                            # pragma: no cover
            out = NugenResult(error=f"Briefing thread failed: {exc}",
                              max_tokens=budget)
        with _lock:
            _async_out[cid] = out
            _inflight.discard(cid)

    try:
        threading.Thread(target=_work, name=f"nugen-{role or 'brief'}",
                         daemon=True).start()
    except Exception as exc:                            # thread table full
        # Without this the key stays in _inflight for the life of the
        # process: pending_count() never returns to zero, the UI keeps its
        # fast refresh, and this situation reads "preparing" for ever.
        with _lock:
            _inflight.discard(cid)
            _async_out[cid] = NugenResult(
                error=f"Could not start the briefing thread: {exc}",
                max_tokens=budget)
        return _async_out[cid]
    return None


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
# The question is bounded BEFORE the facts are appended to it. complete()
# truncates the prompt from the end, so an unbounded question would evict the
# whole FACTS block and leave the model instructed to answer "using only the
# FACTS given" with no facts present at all.
QUESTION_CHAR_LIMIT = 400


@dataclass
class Fact:
    """One field read off the computed state, with where it came from."""
    field: str              # the attribute name — the provenance
    label: str
    value: str
    clause: str = ""        # the field as a sentence fragment, for the answer
    score: int = 0          # how many of the question's topics it covers
    rank: int = 0           # operational salience; see _RANK
    breadth: int = 0        # how many topics it belongs to at all


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
                    and not self.result.unsupported
                    and not self.result.hazards)


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
     ("core", "overall", "flood", "levee", "people", "timing"), "{l} is {v}"),
    ("drought_sev", "Drought severity", _sev("drought_sev"),
     ("core", "overall", "drought", "crop", "soil"), "{l} is {v}"),
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

# Tie-break within an equal topic match: which field a duty officer reads
# first. Breadth alone was doing this job and did it backwards — asked for the
# drought situation, potential and actual evapotranspiration (two topics each)
# outranked the evaporative stress percentile and days-to-wilting (four each),
# so the answer opened on two figures nobody acts on. Salience is a judgement,
# so it is written down once, here, where it can be argued with.
_RANK: dict = {
    # the two verdicts
    "flood_sev": 0, "drought_sev": 0,
    # what a desk acts on
    "time_to_overtop_min": 1, "households_at_risk": 1, "inflow_peak": 1,
    "reservoir_pct": 1, "firo_release": 1, "esp": 1, "days_to_wilting": 1,
    "real_compute_ms": 1, "kge": 1,
    # supporting readings fall through to _DEFAULT_RANK
    # ambient conditions, last
    "temp": 3, "pet": 3, "et_actual": 3, "lead_time_days": 3,
}
_DEFAULT_RANK = 2

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
              "danger*"),
    # Hazard-neutral words. They belong to BOTH hazards, so filing them under
    # "flood" (as they were) answered "what is the drought situation?" with
    # the whole flood side. Only the two severity fields carry this topic, so
    # it anchors an answer without biasing it towards either hazard.
    "overall": ("severity", "alert", "alerts", "emergency", "situation",
                "how bad", "status", "risk", "risks", "overall"),
    # "left" is deliberately absent: it made "how much storage is left" a
    # question about time-to-overtopping. "how long", "until", "when" and
    # "time to" already cover every temporal reading of the word.
    "timing": ("how long", "when", "time to", "minute*", "hour*", "soon",
               "until", "eta", "arrive*", "arriving", "before", "deadline"),
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
    "action_dam": ("duty engineer*", "engineer*", "control room",
                   "control-room", "operator*", "gate team*"),
    "action_disaster": ("disaster*", "ndrf", "rescue", "response team*",
                        "district officer*"),
    "action_farmer": ("farmer*", "grower*", "cultivator*"),
    # -- the three assistance jobs ------------------------------------------
    "safe_place": ("where to go", "where should", "where do", "safe place*",
                   "safe ground", "safe to", "shelter*", "refuge*", "muster*",
                   "assembly*", "high ground", "higher ground", "relocat*",
                   "move to", "take shelter", "destination*", "camp*",
                   "evacuate to", "go to"),
    "mitigation": ("mitigat*", "measure*", "reduce", "reducing", "minimis*",
                   "minimiz*", "protect*", "prepare*", "preparation*",
                   "precaution*", "damage", "losses", "loss", "prevent*",
                   "what else", "checklist", "steps", "action*", "do about",
                   "cope", "coping"),
    "alert": ("alert*", "pattern*", "trend*", "signal*", "notice*", "noticed",
              "flagged", "anything unusual", "detect*", "warn me",
              "worth knowing", "watch out", "changed"),
}

# Naming a desk implies that desk's data: "what should the duty engineer do?"
# is a question about release and storage, not only about the standing order.
_DESK_TOPICS: dict = {
    "action_dam": ("release", "reservoir"),
    "action_disaster": ("people", "levee"),
    "action_farmer": ("crop", "soil", "drought"),
    # Asking where people go is asking about who is exposed and by when;
    # answering it out of the two severity readings alone, which is what
    # happened before, is not an answer.
    "safe_place": ("people", "levee", "timing"),
    "mitigation": ("overall",),
    "alert": ("overall",),
}

# Phrasings that make a question about what to DO rather than what IS. When one
# fires and no single desk was named, all three standing orders are retrieved --
# "what do we do now" is a question about every desk.
_ACTION_WORDS = ("what should", "what do i", "what do we", "should i",
                 "should we", "what action", "recommend", "advice", "advise",
                 "next step", "do now", "what now", "instruction*", "order*",
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

# Words that carry no domain content of their own.
#
# An action question built only from these is a question about the standing
# orders — "what should we do now?" is a fair thing to ask a control room. An
# action question wrapped round a noun this console has never heard of is not
# about the basin at all. Without the distinction, the bare ``action`` topic was
# enough to pass the scope gate, and "what should I cook for dinner?" was
# answered with an evacuation order: the exact failure the scope step exists to
# prevent, and a very bad look on a flood console.
_FILLER = frozenset("""
a about action actions advice advise an and any are as at be been being best by
can could did do does doing done for from get give given go going good got
guidance had has have he her him his how i if in instruction instructions into
is it its just know let like make may me more most much must my need needs next
now of on one only or order orders other our out over please recommend
recommendation said say see shall she should so some step steps such take tell
than that the their them then there these they thing things think this those to
told too us use very want was way we well were what when where which while who
whom why will with would you your
""".split())


def _off_domain_words(question: str) -> list:
    """Content words the console has no vocabulary for at all.

    A word counts as known if any topic pattern matches it — which reuses the
    very same patterns that drive retrieval, so the vocabulary cannot drift
    away from the fields that actually exist.
    """
    out = []
    for word in re.findall(r"[a-z]+", str(question or "").lower()):
        if len(word) < 3 or word in _FILLER:
            continue
        if any(pat.search(word)
               for pats in _TOPIC_RE.values() for pat in pats):
            continue
        out.append(word)
    return out

# One question per job, plus one per desk. Deliberately none of them asks what
# is going to happen: the first entry used to be "How long until the levee
# overtops, and how many households are exposed?", which is the console offering
# an operator a one-click route to the forecast this layer must not produce.
SUGGESTED_QUESTIONS = (
    "Where should residents go, and in what order?",
    "What else should we be doing to reduce the damage?",
    "What patterns in the data are worth knowing about?",
    "What should the duty engineer do with the gates right now?",
    "Should farmers irrigate this week, and what else protects the crop?",
    "How many households are inside the mapped low-lying zone?",
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
        # An action phrase on its own asks about the standing orders, and
        # "what should we do now?" deserves an answer. An action phrase
        # wrapped round a noun this console has never heard of does not: on
        # its own the bare "action" topic was enough to pass the scope gate,
        # and "what should I cook for dinner?" was answered with an
        # evacuation order.
        if found or not _off_domain_words(q):
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
    if not topics:
        # Nothing matched, so nothing is read. Returning the "core" fields
        # here would leave an out-of-scope answer carrying two readings while
        # its own harness row reported that nothing had been retrieved.
        return [], topics
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
            score=len(asked.intersection(field_topics)),
            rank=_RANK.get(attr, _DEFAULT_RANK),
            breadth=len(field_topics)))

    # Which desks the question actually reaches. Only when it reaches none of
    # them does an action phrase mean "every desk" — "what should we do now?"
    # is a question about all three, but "should I irrigate my field?" is a
    # question for the farmer, and used to come back with the gate schedule
    # and the evacuation order attached.
    desk_hit = {role: asked.intersection(order_topics)
                for role, _, order_topics in _ORDERS}
    any_desk = any(desk_hit.values())
    for role, label, order_topics in _ORDERS:
        hit = desk_hit[role]
        if not hit and not (action and not any_desk):
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
                          score=len(hit) + (3 if action else 0),
                          rank=0, breadth=len(order_topics)))

    # Score first, then SPECIFICITY. Score alone left ties to be broken by
    # registry order, which is flood-first: asked "how much storage is left",
    # flood severity and the levee won the tie and the storage fields were cut
    # out of the four-clause answer they were retrieved for. A field belonging
    # to one topic is a sharper answer than a five-topic catch-all.
    facts.sort(key=lambda f: (-f.score, f.rank, f.breadth))
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


# The instruction, and the one thing it forbids above all.
#
# The prohibition is not decoration. Earlier versions of this file told the
# model to describe "what the inflow is doing" and "how long there is", and
# demonstrated both in their worked examples — which is to say they instructed a
# language model to forecast a flood. The hazard call belongs to the physics
# engine and to nothing else; this layer is for what to do about a call that has
# already been made. ``states_a_hazard`` is the backstop for when the model
# ignores this anyway, and at that point the wording is discarded.
ANALYST_SYSTEM = (
    "You are the duty analyst in a river-basin flood and drought control room. "
    "Answer the operator's question in two or three short sentences, using ONLY "
    "the FACTS given. Quote the exact numbers from the FACTS with their units. "
    "Never add, estimate, convert or change a number, and never invent an "
    "instruction. "
    "The flood and drought situation has ALREADY been decided by the control "
    "room's own calculations and is given to you as settled fact. Never restate "
    "it as your own conclusion, never predict what the river, the rain, the "
    "soil or the crop will do next, and never say that something will happen, "
    "is expected, is likely or is imminent. Report only what the facts already "
    "show, and what is to be done about it. "
    "If the FACTS do not answer the question, say that this console does not "
    "hold that figure. Do not greet, do not repeat the question back, and do "
    "not list the facts."
)

# The worked example. It is the single thing a small instruct model
# pattern-matches against, so it has to demonstrate the compliant shape rather
# than merely be adjacent to a rule asking for it. Note what it does with the
# severity it is handed: it acts on it, and never re-announces it.
_ANALYST_EXAMPLE_Q = (
    "QUESTION: Who moves first, and what else should we be doing?\n"
    "FACTS:\n"
    "Basin: Krishna Basin (Sangli). Time: 02 Aug 2026, 09:00 IST.\n"
    "Flood severity (already decided): WARNING\n"
    "Households inside the mapped low-lying zone: 1,030 households\n"
    "Order of movement: Riverfront first, then Low Road, then Market\n"
    "Measure [now]: Start with the people who cannot start themselves.\n"
    "Measure [now]: Cut power and isolate the pumping station in the lowest "
    "zone.\n"
    "Noticed in the data: Downstream flow is 870 m3/s, 145% of the 600 m3/s "
    "safe channel capacity."
)
_ANALYST_EXAMPLE_A = (
    "Move the Riverfront zone first, then Low Road, then Market \u2014 1,030 "
    "households sit inside the mapped low-lying zone across the three. Start "
    "with the people who cannot start themselves, and cut power to the pumping "
    "station in the lowest zone before anyone wades it. Downstream flow is "
    "already 870 m3/s against a 600 m3/s safe channel, so the margin reported "
    "in the data has gone."
)


def _bounded(question: str) -> str:
    """One line of question, short enough that the facts cannot be evicted."""
    text = " ".join(str(question or "").split())
    return (text if len(text) <= QUESTION_CHAR_LIMIT
            else text[:QUESTION_CHAR_LIMIT].rstrip() + "...")


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
    lines = [f"QUESTION: {_bounded(question)}", "FACTS:", head]
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
    question = _bounded(question)
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
    # Truncate here, not in complete(): the messages are built from this
    # string and unsupported_numbers() verifies the reply against it, so the
    # prompt sent and the guard applied have to be the same bytes.
    out.digest = build_analyst_digest(facts, question, place_name,
                                      getattr(state, "clock", "")
                                      )[:_PROMPT_CHAR_LIMIT]
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
         "the answer of record, and it stands whether or not anything "
         "else runs."),
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

    # No model id is passed: ``complete`` resolves the catalogue itself and
    # picks the analyst slot from ``role``. Choosing it here instead would
    # have to read ``known_models`` before resolution has happened, which is
    # how the first question of a process ended up on the briefing model and
    # cached under a key the second ask never looked up.
    res = complete(out.digest, api_key=api_key, max_tokens=ANALYST_MAX_TOKENS,
                   role=ANALYST_ROLE,
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
