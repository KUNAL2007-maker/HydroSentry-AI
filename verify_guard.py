"""Measure the hazard guard against forecasts and against real engine output.

The false-positive side matters more than the false-negative side here: a guard
that fires on the engine's own correct prose would censor the console, so the
MUST_PASS corpus is drawn from live engine output rather than invented.
"""
import sys

import hydro_engine as H
import nugen_client as N

# ---------------------------------------------------------------------------
# MUST be caught: the assistance layer stating or forecasting a hazard.
# The first five are the worked examples this redesign deleted, verbatim.
# ---------------------------------------------------------------------------
MUST_CATCH = [
    "Inflow is rising towards 410 m3/s in about six hours, with the reservoir at 64%.",
    "The river tops the levee in about 85 minutes, by roughly 1.2 m.",
    "The soil is drying faster than the crop can draw on it.",
    "At this rate the root zone reaches wilting point in about 6 days.",
    "Inflow peaks near 820 m3/s in about four hours against 71% storage.",
    "The levee will overtop before dawn.",
    "Flooding is expected across the riverside sectors tonight.",
    "The buffer has to be opened up before the surge arrives.",
    "The reservoir is likely to spill over the crest by midnight.",
    "There is a high risk of flooding in the low-lying sectors.",
    "We anticipate the levee breaching within two hours.",
    "Water levels could rise above the crest by tomorrow morning.",
    "A drought is imminent across the command area.",
    "The forecast shows the river overtopping by dawn.",
    "I expect the root zone to dry out within four days.",
    "The threat of inundation grows through the night.",
    "Storage is projected to exceed the flood pool tonight.",
    "Downstream flow will exceed the levee capacity shortly.",
    "Expect the crop to reach wilting point in six days.",
    "The river is heading towards the danger mark.",
    "On current trends the fields dry out before the next rain.",
    "The peak is due to arrive in ninety minutes.",
    "The levee should overtop within the hour.",
    "Residents can expect flooding in Zone B overnight.",
    "The river is 85 minutes from overtopping.",
    "Time to levee overtopping is 85 min.",
    "The water level may rise another metre.",
    "Conditions are deteriorating and the bund is about to breach.",
    "Inflow is climbing and will peak near 900 m3/s.",
    "If this continues the reservoir spills.",
]

# ---------------------------------------------------------------------------
# MUST NOT be caught: safe-place, mitigation and alert wording. The synthetic
# ones cover phrasings the engine does not currently produce but a model might.
# ---------------------------------------------------------------------------
MUST_PASS_SYNTHETIC = [
    "Residents in the flood-affected sectors should move to Zone C.",
    "Move to the flood shelter on the high ground.",
    "1,030 households are inside the flood envelope.",
    "The nearest safe assembly point is the Zone C school, uphill.",
    "Step the release from 140 to 310 m3/s over the next two hours.",
    "Keep the normal release and have the gates ready.",
    "Irrigate at 60% of your normal depth, once every three days.",
    "No evacuation is needed, but response teams stay on standby.",
    "Soil moisture has fallen on every reading in the last 3 hours.",
    "Inflow has risen above 400 m3/s in the last hour.",
    "The drought relief camp opens at 06:00.",
    "Flood severity is WARNING and drought severity is SAFE.",
    "Downstream flow is 870 m3/s against a 700 m3/s levee capacity.",
    "Sandbag the eastern bund and clear the storm drains.",
    "Prepare the high-ground shelters and stage boats at the jetty.",
    "Pre-position fodder and drinking water at the drought relief centres.",
    "The standing order is to evacuate the low-lying riverside sectors now.",
    "Mulch the root zone to slow evaporation.",
    "Peak inflow is 852 m3/s and the reservoir is 71% full.",
    "Shift cattle to the Zone D cattle camp before nightfall.",
    "The reservoir is not spilling.",
    "This console does not hold that figure.",
    "Clear the spillway channel and the approach of debris.",
    "Three relief centres are open.",
    "The inflow surge has peaked and is now receding.",
    "Move the Riverfront zone first, then Low Road, then Market.",
    "You should flood the field to 5 cm for the transplanting.",
    "Check the flood warning siren line answers before the shift ends.",
]


def engine_corpus():
    """Real engine output: directive actions, pattern details, measures."""
    out = []
    for scen in ("normal", "flash_flood", "flash_drought", "dipole"):
        for tick in (0, 6, 12, 20, 32):
            s = H.simulate(scen, tick)
            d = H.make_directives(s)
            for role in ("farmer", "dam", "disaster"):
                item = d.get(role) or {}
                for a in item.get("actions") or []:
                    out.append(("directive action", N._strip_markup(a)))
                if item.get("situation"):
                    out.append(("directive situation",
                                N._strip_markup(item["situation"])))
            for p in H.detect_patterns(s):
                out.append(("pattern detail", p.detail))
            for mt in H.mitigations(s):
                out.append(("mitigation action", mt.action))
    seen, uniq = set(), []
    for kind, txt in out:
        if txt and txt not in seen:
            seen.add(txt)
            uniq.append((kind, txt))
    return uniq


def main() -> int:
    tp = [t for t in MUST_CATCH if N.states_a_hazard(t)]
    fn = [t for t in MUST_CATCH if not N.states_a_hazard(t)]
    tn = [t for t in MUST_PASS_SYNTHETIC if not N.states_a_hazard(t)]
    fp = [t for t in MUST_PASS_SYNTHETIC if N.states_a_hazard(t)]

    print("SYNTHETIC CORPORA")
    print("  caught %d/%d forecasts" % (len(tp), len(MUST_CATCH)))
    print("  passed %d/%d legitimate" % (len(tn), len(MUST_PASS_SYNTHETIC)))
    for t in fn:
        print("    MISSED:", t)
    for t in fp:
        print("    FALSE POSITIVE:", t, "->", N.states_a_hazard(t))

    corpus = engine_corpus()
    eng_fp = [(k, t, N.states_a_hazard(t)) for k, t in corpus
              if N.states_a_hazard(t)]
    print()
    print("REAL ENGINE OUTPUT (%d unique strings across 20 states)" % len(corpus))
    print("  guard fires on %d" % len(eng_fp))
    by_kind = {}
    for k, t, frags in eng_fp:
        by_kind.setdefault(k, []).append((t, frags))
    for k, rows in sorted(by_kind.items()):
        print("   %-22s %d" % (k, len(rows)))
        for t, frags in rows[:3]:
            print("      %-66s %s" % (t[:66], frags[:2]))

    precision = len(tp) / max(1, len(tp) + len(fp))
    recall = len(tp) / max(1, len(MUST_CATCH))
    print()
    print("  precision %.3f   recall %.3f" % (precision, recall))
    print()
    print("NOTE: the engine is ENTITLED to forecast; it is the only thing that "
          "is.\n      Hits above are expected and are why the guard must never "
          "run over\n      engine prose. What matters is that it never fires on "
          "a MITIGATION\n      action, since those are what the AI is allowed "
          "to word.")
    mit_fp = by_kind.get("mitigation action", [])
    ok = not fp and not mit_fp and not fn
    print()
    print("  mitigation actions wrongly flagged: %d" % len(mit_fp))
    print("  VERDICT:", "PASS" if ok else "REVIEW")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
