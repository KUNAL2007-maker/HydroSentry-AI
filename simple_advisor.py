"""simple_advisor.py — plain-language crisis advice, grounded in the engine.

What this file is
-----------------
The engine (hydro_engine.simulate / make_directives) decides WHETHER there
is a flood or a drought and how bad it is. This file only turns that decision
into words a normal person can act on. It never invents a hazard, a place, a
number, or a shelter:

* every number comes from the BasinState ``s`` or the directives ``d`` that
  were passed in (inflow, time to overtop, households at risk, wilting days);
* the only shelter named is ``place.shelter_phrase`` when a Place was given,
  otherwise the generic "the shelter in your district plan";
* the irrigation steps come from ``d["farmer"]["actions"]`` — the engine's
  own directive, with its HTML tags stripped — never from a model.

If the AI wording layer is unavailable, the caller shows these cards as they
are. They are the answer of record, not a fallback apology.

Usage:  crisis_advice(s, d, place=None) -> list[dict]
Each dict: {level, title, what, why, steps, who}
level is one of: safe | watch | warning | critical.
"""

from __future__ import annotations

import math
import re

import hydro_engine as H

_LEVEL_RANK = {"critical": 0, "warning": 1, "watch": 2, "safe": 3}
_GENERIC_SHELTER = "the shelter in your district plan"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def _plain_time(minutes) -> str:
    """Time-to-overtop in plain words, or '' when there is no overtop."""
    try:
        mins = float(minutes)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(mins):
        return ""
    if mins <= 1:
        return "right now"
    if mins < 60:
        return f"in about {int(round(mins))} minutes"
    hours = mins / 60.0
    if hours < 1.5:
        return "in about an hour"
    if hours < 2.5:
        return "in about an hour and a half"
    return f"in about {int(round(hours))} hours"


def _plain_days(days) -> str:
    try:
        d = float(days)
    except (TypeError, ValueError):
        return "not expected soon"
    if not math.isfinite(d):
        return "not expected soon"
    if d <= 1:
        return "tomorrow"
    return f"in about {int(round(d))} days"


def _shelter(place) -> str:
    phrase = getattr(place, "shelter_phrase", None) if place is not None else None
    return phrase.strip() if phrase else _GENERIC_SHELTER


def _farmer_steps(d) -> list:
    """The engine's own farmer actions, HTML stripped, in plain words."""
    try:
        actions = (d or {}).get("farmer", {}).get("actions") or []
    except AttributeError:
        actions = []
    return [_strip_html(a) for a in actions if _strip_html(a)]


def crisis_advice(s, d, place=None) -> list:
    """Plain-language crisis cards for this basin state, most serious first.

    ``s`` is a hydro_engine BasinState, ``d`` is hydro_engine.make_directives(s).
    ``place`` is a hydro_engine Place (or None: the shelter is then named only
    as "the shelter in your district plan"). The caller in app.py passes the
    scripted DEMO_PLACE in demo mode, so the demo may name its own shelter;
    a live region names only its own district phrase, via place_from_region.
    """
    flood = getattr(s, "flood_sev", "safe") or "safe"
    drought = getattr(s, "drought_sev", "safe") or "safe"
    shelter = _shelter(place)
    inflow = int(round(float(getattr(s, "inflow_peak", 0) or 0)))
    households = int(getattr(s, "households_at_risk", 0) or 0)
    tto = _plain_time(getattr(s, "time_to_overtop_min", float("inf")))
    wilt = _plain_days(getattr(s, "days_to_wilting", float("inf")))
    cards = []

    # ---- flood ------------------------------------------------------------
    if flood == "critical":
        when = f" The river will rise above the levee {tto}." if tto else ""
        cards.append({
            "level": "critical",
            "title": "Move to high ground now",
            "what": (
                f"The flood is critical. Water is coming in at {inflow:,} cubic "
                f"metres per second.{when} About {households:,} families are in "
                "the low-lying area."
            ),
            "why": "The engine says the river will go over the levee. Do not wait for the water to reach your house.",
            "steps": [
                "Move to high ground or an upper floor now — do not wait.",
                "Do NOT drive or walk through flood water. Even shallow water can knock you down or carry a car away.",
                "Take your phone, documents, drinking water and medicines with you.",
                f"Go to {shelter}.",
                "Help elderly people and children first.",
                "Stay on high ground until the authorities say it is safe to return.",
            ],
            "who": "Everyone in the low-lying area, especially elderly people and children",
        })
    elif flood == "warning":
        when = f" The river could rise above the levee {tto}." if tto else ""
        cards.append({
            "level": "warning",
            "title": "Get ready to move to high ground",
            "what": (
                f"The flood risk is high. Water is coming in at {inflow:,} cubic "
                f"metres per second.{when}"
            ),
            "why": "The water is rising fast. Pack now, so you can leave quickly if it gets worse.",
            "steps": [
                "Pack a bag: phone, documents, drinking water and medicines.",
                "Keep your phone charged and stay near high ground.",
                f"Know your way to {shelter} before you need it.",
                "Do NOT drive or walk through flood water.",
                "Help elderly people and children first.",
            ],
            "who": "Everyone in the low-lying area",
        })
    elif flood == "watch":
        cards.append({
            "level": "watch",
            "title": "Flood risk is building — stay alert",
            "what": f"Water is coming in at {inflow:,} cubic metres per second — more than usual, but the river is still inside the levee.",
            "why": "The engine is watching the river. Nothing to do yet, but be ready.",
            "steps": [
                "Check this page again in an hour.",
                "Keep your phone charged.",
                "Stay away from the riverbank and low roads.",
            ],
            "who": "People near the river",
        })

    # ---- drought ----------------------------------------------------------
    if drought in ("warning", "critical"):
        level = "critical" if drought == "critical" else "warning"
        steps = _farmer_steps(d)
        # The engine's farmer actions already say "45 minutes at 4:00 AM";
        # keep them verbatim (HTML stripped) and add the plain save-water step.
        steps = steps[:3] + [
            "Save water at home: fix leaks, and reuse water where you can.",
            "Do not water crops in the middle of the day — most of it dries up before the roots can use it.",
        ]
        cards.append({
            "level": level,
            "title": "Save water — crops are drying out",
            "what": (
                f"The soil is drying out. At this rate the crops will start to "
                f"wilt {wilt}."
            ),
            "why": "Hot, dry air is pulling water out of the soil faster than the crop can cope with.",
            "steps": steps,
            "who": "Farmers, and everyone using water in the area",
        })
    elif drought == "watch":
        cards.append({
            "level": "watch",
            "title": "Soil is drying — watch your crops",
            "what": "The soil is starting to dry out, but the crops are not in danger yet.",
            "why": "The engine is watching the soil. A little care now can save the crop later.",
            "steps": [
                "Check your soil in the morning.",
                "Water only in the early morning if the soil feels dry.",
                "Check this page again tomorrow.",
            ],
            "who": "Farmers",
        })

    # ---- both at once (dipole) --------------------------------------------
    if (flood in ("warning", "critical") and drought in ("warning", "critical")):
        cards.append({
            "level": "warning",
            "title": "Flood and drought at the same time",
            "what": (
                "This area has both problems at once: the river is rising in "
                "one part, while the soil is drying out in another."
            ),
            "why": "Flood safety comes first — water can hurt people today. Saving water protects the crop for next week.",
            "steps": [
                "First, do the flood steps above: move to high ground if you are in the low area.",
                "Then, save water: irrigate at 4 AM for 45 minutes, and do not water in the middle of the day.",
                "Do not use flood water for drinking or for crops.",
            ],
            "who": "Everyone in the area — flood first, then water saving",
        })

    # ---- nothing happening --------------------------------------------------
    if not cards:
        cards.append({
            "level": "safe",
            "title": "No crisis right now",
            "what": "The flood risk is low and the soil has enough water. There is nothing you need to do today.",
            "why": "The engine checked the river and the soil, and both are normal.",
            "steps": [
                "Check this page again tomorrow.",
                "Know where the high ground is near your home, just in case.",
                "Keep your phone charged.",
            ],
            "who": "Everyone in the area",
        })

    cards.sort(key=lambda c: (_LEVEL_RANK[c["level"]], 0 if "high ground" in c["title"].lower() else 1))
    return cards
