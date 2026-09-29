"""verify_demo_golden.py — demo-output regression guard.

The demo mode is a competition deliverable: its output must stay byte-identical
across changes. This compares ``make_directives(simulate(scenario, tick))`` for
all 4 scenarios x ticks 0..TICKS_MAX against the frozen snapshot in
``_demo_golden.json`` (132 states, keyed "{scenario}:{tick}").

Run:  python verify_demo_golden.py
Exit: 0 = all states identical, 1 = at least one mismatch.
"""

from __future__ import annotations

import json
import sys

import hydro_engine


def _dump(scenario: str, tick: int) -> str:
    s = hydro_engine.simulate(scenario, tick)
    return json.dumps(hydro_engine.make_directives(s), sort_keys=True, ensure_ascii=False)


def main() -> int:
    with open("_demo_golden.json", encoding="utf-8") as fh:
        golden = json.load(fh)

    checked = 0
    mismatches = []
    for scen in hydro_engine.SCENARIOS:
        for tick in range(hydro_engine.TICKS_MAX + 1):
            key = f"{scen}:{tick}"
            if key not in golden:
                mismatches.append((key, "MISSING FROM GOLDEN"))
                continue
            checked += 1
            if _dump(scen, tick) != golden[key]:
                mismatches.append((key, "OUTPUT CHANGED"))

    print(f"demo golden: {checked - len(mismatches)}/{len(golden)} states identical "
          f"({len(mismatches)} mismatches)")
    for key, why in mismatches[:10]:
        print(f"  MISMATCH {key}: {why}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
