"""
nugen_client.py — optional plain-language briefing layer (Nugen inference API)
==============================================================================

WHAT THIS IS
------------
A thin, defensive client for the Nugen chat-completions endpoint. It takes the
directives the deterministic rule engine has *already* produced and asks a small
hosted model to re-word them into one short operator briefing.

WHAT THIS IS NOT
----------------
It is **not** part of the forecast. It never computes, predicts or changes a
number — every figure it is allowed to mention is handed to it in the prompt,
and if the call fails, is not configured, or is out of budget, the console is
unaffected: the rule-based directives are the product, this is a presentation
layer on top of them.

QUOTA DISCIPLINE (the account allows 500 tokens per completion)
---------------------------------------------------------------
* ``MAX_TOKENS_LIMIT = 500`` is enforced with ``min()`` — a caller cannot
  request more, whatever it passes.
* The default request is smaller still (``DEFAULT_MAX_TOKENS``) because an
  operator briefing is a few sentences, not an essay.
* The prompt is built from a compact digest of the state and hard-truncated
  (``_PROMPT_CHAR_LIMIT``) so input tokens stay bounded too.
* Results are cached in-process on a hash of the exact prompt, so the 60-second
  live refresh re-renders the briefing for free instead of re-billing it.
* ``CALL_BUDGET`` caps how many *network* calls this process will ever make, so
  a redeploy loop or a stuck refresh cannot drain the quota.
* Nothing calls this module automatically — the UI invokes it on a click.

SECRETS
-------
The key is read from the ``NUGEN_API_KEY`` environment variable (or passed in by
the caller, which is how the app forwards ``st.secrets``). It is never written
to the repository. On Render, set it under *Environment → Environment Variables*.
"""

from __future__ import annotations

import hashlib
import json
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
    """Drop cached briefings (the call budget is deliberately NOT reset)."""
    with _lock:
        _cache.clear()


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


def build_messages(digest: str) -> list[dict]:
    """The exact message list sent to the endpoint for a set of facts."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _EXAMPLE_FACTS},
        {"role": "assistant", "content": _EXAMPLE_BRIEFING},
        {"role": "user", "content": digest},
    ]


def transcript(digest: str) -> str:
    """Human-readable rendering of ``build_messages`` for the UI's audit panel."""
    return "\n\n".join(f"[{msg['role']}]\n{msg['content']}"
                       for msg in build_messages(digest))


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
    if echoed >= 2 or "Dam order:" in body or "Disaster order:" in body:
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


def complete(digest: str, api_key: str | None = None,
             max_tokens: int = DEFAULT_MAX_TOKENS,
             use_cache: bool = True) -> NugenResult:
    """One briefing request for a block of facts. Never raises.

    ``max_tokens`` is clamped to ``MAX_TOKENS_LIMIT`` (500) regardless of what
    the caller asks for, the facts are truncated to ``_PROMPT_CHAR_LIMIT``, and
    a repeat of the same facts is served from the in-process cache without
    spending a call.
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

    cache_id = hashlib.sha256(
        f"{MODEL}|{budget}|{digest}".encode("utf-8")).hexdigest()
    if use_cache:
        with _lock:
            hit = _cache.get(cache_id)
        if hit is not None:
            return NugenResult(ok=hit.ok, text=hit.text, error=hit.error,
                               cached=True, model=hit.model,
                               max_tokens=hit.max_tokens, usage=dict(hit.usage),
                               unsupported=list(hit.unsupported))

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
        "messages": build_messages(digest),
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
