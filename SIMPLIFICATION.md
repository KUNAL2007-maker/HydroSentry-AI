# HydroSentry-AI, simplified

**The short version:** the maths did not change. The screen did. A normal
person now understands the basin in about 10 seconds, and a flood warning
now tells them what to do — in plain words.

## What a normal person sees in 10 seconds

Open the app and there are three tabs, in plain language:

1. **"Are we safe?"** — one big banner in plain words: "All clear", "Keep an
   eye on it", "Be ready", or "Flood — move to high ground now". Under it,
   just four simple numbers:
   - **Flood risk** — Low / Building / High / Very high
   - **Water coming in** — how much water is flowing into the river
   - **Time left** — "about 90 minutes", "right now", or "plenty of time"
   - **Soil / crops** — OK, Getting dry, or Dry — and when the crops would wilt
2. **"What should I do?"** — three simple cards: one for farmers, one for the
   dam operator, one for the disaster team. Each card says what to do in 3
   steps, in plain words.
3. **"Crisis help (AI)"** — advice cards for the crisis happening right now,
   most urgent first. See below.

No KGE, POD, ESP percentile, mass-balance, or "53th %ile" on the main screen.
On a phone, everything stacks into one column and the banner and buttons
stretch full width — nothing overflows.

## What the AI tab now does in a crisis

The AI no longer sits on a tab full of tables. It does one job: when there
is a crisis, it helps people act.

**Flood example.** If the engine says the flood is critical, the top card is:

> **Move to high ground now**
> The flood is critical. Water is coming in at 852 cubic metres per second.
> The river will rise above the levee in about an hour and a half. About
> 1,030 families are in the low-lying area.
> 1. Move to high ground or an upper floor now — do not wait.
> 2. Do NOT drive or walk through flood water.
> 3. Take your phone, documents, drinking water and medicines.
> 4. Go to the shelter in your district plan.
> 5. Help elderly people and children first.

There is also a button, "Say this again in simpler words (AI)". Pressing it
asks the AI to re-word **only that top card**, using the numbers the engine
already computed. If the AI is not set up, or its wording quotes a number the
engine never computed, you see the engine's own words instead, labelled
"From the engine, not AI". The AI never decides whether there is a flood —
the engine does that. The AI never names a shelter the district plan did not
name.

The same works for drought: the top card says "Save water — crops are drying
out", with the engine's own advice to irrigate at 4:00 AM for 45 minutes.
If flood and drought happen at once, there is a card for that too: flood
safety first, then saving water.

## What moved, and where it went

- **To the backend (unchanged, still running):** all the physics — the
  rainfall routing, the reservoir mass balance, the levee and wilting
  calculations, and the model scores (KGE, POD, mass balance). Nothing about
  them changed; they just stopped being the first thing on the screen.
- **To one collapsed box, "For engineers — how we calculated this", at the
  bottom of the third tab:** the old analyst tables (patterns in the data,
  where to go, what else to do), the ask-the-analyst question box, the
  engine's live self-check, the comparison with conventional modelling, and
  the full old five-tab detail. Engineers can still open it and check
  everything. Normal users never have to.
- **Sidebar, tidied:** only Mode (Live / Demo), Place (preset + search), and
  in demo mode the scenario and Step / Restart. Live readings, Data sources
  and Severity scale are collapsed boxes. The dam-operator slider is in a
  collapsed box called "Dam operator (optional)" — and it is shown in **both**
  modes now, which also fixes a bug: before, switching Live → Demo → Live
  made the reservoir reading reset to 0% (from 78%), because the slider
  disappeared in demo mode. It cannot disappear any more.

## What did NOT change

- The engine: `hydro_engine.py` is untouched. `simulate()` and
  `make_directives()` behave exactly as before.
- The numbers: every figure on the simple screen comes from the same engine
  state — inflow, time to overtop, households at risk, wilting days.
- The tests: `verify_demo_golden.py` still passes 132/132, `verify_fixes.py`
  50/50, and the new `verify_simple_advisor.py` passes 25/25.

## How to run it

Locally (same as before, no new software to install):

```bash
pip install -r requirements.txt
streamlit run app.py
```

Deploy: the same Render command as before (`streamlit run app.py`), no new
environment variables. The AI re-wording is optional: without
`NUGEN_API_KEY` it is simply not offered, and the fixed engine wording is
shown instead. Nothing else changes.
