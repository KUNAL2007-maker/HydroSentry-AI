"""verify_simple_advisor.py — checks for simple_advisor.crisis_advice().

Builds the four demo scenarios through hydro_engine.simulate() (the same
engine the dashboard uses) and asserts the plain-language cards are grounded:
a flash flood must say "move to high ground", a flash drought must say
"save water", a normal day must have no critical card, and no card may name
a shelter the engine never gave it.

Run:  python verify_simple_advisor.py
Exit: 0 = all checks passed, 1 = at least one failed.
"""

from __future__ import annotations

import sys

import hydro_engine as H
from simple_advisor import crisis_advice

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


def _cards(scenario, tick):
    s = H.simulate(scenario, tick)
    d = H.make_directives(s)
    return s, d, crisis_advice(s, d)


def _text(cards):
    return "\n".join(
        (c["title"] + " " + c["what"] + " " + c["why"] + " " + " ".join(c["steps"]))
        for c in cards
    )


def main() -> int:
    # ---- flash flood: critical card, high ground, real numbers ------------
    s, d, cards = _cards("flash_flood", 0)
    check("flood: a card is returned", len(cards) >= 1, str(len(cards)))
    top = cards[0]
    check("flood: top card is critical", top["level"] == "critical", top["level"])
    check("flood: top card says move to high ground",
          "Move to high ground now" in top["title"], top["title"])
    check("flood: steps say do not drive/walk through flood water",
          any("Do NOT drive or walk through flood water" in st for st in top["steps"]),
          str(top["steps"]))
    check("flood: time to overtop is in plain words (about 90 minutes)",
          "in about an hour and a half" in top["what"], top["what"])
    check("flood: households at risk are stated as families",
          "1,030 families" in top["what"], top["what"])
    check("flood: inflow number comes from the engine",
          f"{int(round(s.inflow_peak)):,} cubic" in top["what"], top["what"])

    # ---- flash drought: a drought card, with the engine's 4 AM advice -----
    s, d, cards = _cards("flash_drought", 32)
    levels = [c["level"] for c in cards]
    check("drought: a critical drought card is present",
          "critical" in levels, str(levels))
    drought = [c for c in cards if "Save water" in c["title"]]
    check("drought: card is titled save water", len(drought) == 1, str(len(drought)))
    check("drought: steps carry the engine's 4 AM irrigation advice",
          any("4:00 AM" in st for st in drought[0]["steps"]) if drought else False,
          str(drought[0]["steps"]) if drought else "no card")
    check("drought: wilting time is stated in plain words",
          "in about 15 days" in drought[0]["what"] or "in about 14 days" in drought[0]["what"],
          drought[0]["what"] if drought else "no card")

    # ---- normal: no critical card ------------------------------------------
    _, _, cards = _cards("normal", 16)
    check("normal: no critical card",
          all(c["level"] != "critical" for c in cards),
          str([c["level"] for c in cards]))
    check("normal: the card says no crisis right now",
          any("No crisis right now" in c["title"] for c in cards),
          str([c["title"] for c in cards]))

    # ---- dipole: both flood and drought are covered -------------------------
    _, _, cards = _cards("dipole", 24)
    text = _text(cards).lower()
    check("dipole: a flood card is present", "high ground" in text, text[:120])
    check("dipole: a drought card is present", "save water" in text, text[:120])
    check("dipole: a both-at-once card is present",
          any("at the same time" in c["title"] for c in cards),
          str([c["title"] for c in cards]))

    # ---- no invented shelter -------------------------------------------------
    # With no Place given, the only shelter phrase allowed is the generic one.
    for scenario, tick in (("flash_flood", 0), ("flash_flood", 16), ("normal", 16)):
        _, _, cards = _cards(scenario, tick)
        for c in cards:
            for st in c["steps"]:
                if "shelter" in st.lower():
                    check(f"no invented shelter ({scenario}:{tick})",
                          "the shelter in your district plan" in st
                          or H.DEMO_PLACE.shelter_phrase in st, st)
    # With a Place given, only that place's own shelter phrase may appear.
    place = H.place_from_region("Nashik, Maharashtra, India")
    s = H.simulate("flash_flood", 0)
    d = H.make_directives(s, place)
    cards = crisis_advice(s, d, place)
    top = cards[0]
    check("place: the card uses the place's own shelter phrase",
          place.shelter_phrase in " ".join(top["steps"]),
          " ".join(top["steps"]))
    check("place: no other shelter name appears",
          "Route H2" not in " ".join(top["steps"]), " ".join(top["steps"]))

    # ---- shape: every card has the fields the UI renders --------------------
    for scenario, tick in (("normal", 0), ("flash_flood", 8),
                          ("flash_drought", 24), ("dipole", 16)):
        _, _, cards = _cards(scenario, tick)
        for c in cards:
            check(f"shape {scenario}:{tick} {c['level']}",
                  set(c) == {"level", "title", "what", "why", "steps", "who"}
                  and c["level"] in ("safe", "watch", "warning", "critical")
                  and c["title"] and c["what"] and len(c["steps"]) >= 3,
                  str(c))

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed "
          f"({len(failed)} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
