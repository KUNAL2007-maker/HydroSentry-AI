"""verify_analyst.py — acceptance checks for the grounded analyst in nugen_client.

WHAT IS BEING GUARDED
---------------------
The analyst answers an operator's question about the live basin. The accuracy
claim rests entirely on the pipeline documented under "THE ANALYST" in
``nugen_client.py``: scope -> retrieve -> ground -> generate -> verify -> fall
back. Every one of those steps has a failure mode that is silent unless it is
tested, and the consequence of a silent failure here is a wrong number in a
flood warning:

* SCOPE      a console that answers "who won the match?" with reservoir
             readings is worse than one that declines.
* RETRIEVE   registry order once pushed ``time_to_overtop_min`` off the end of
             the fact sheet for the question "how long until the levee
             overtops" — the one field being asked about. Ranking, not registry
             order, decides what survives the ``MAX_FACTS`` cut.
* TOPIC MATCH substring matching let "gate" match "irriGATE", routing every
             farmer question through the reservoir-release fields.
* GROUND     the answer of record must exist with the language layer off,
             unconfigured, or over budget. If it does not, the product is a
             chatbot with a hydrology model attached.
* PROMPT     the model may see the question and the retrieved facts, and
             nothing else — no key, no unrelated state.
* NEVER RAISE ``ask`` is documented never to raise. It is wired to a free-text
             box, so hostile input is the normal case, not the edge case.

NO NETWORK, BY CONSTRUCTION
---------------------------
There is no Nugen key in this environment and the endpoint must never be
contacted. Every check runs with ``use_model=False`` or ``api_key=''``, and on
top of that ``requests.post``/``requests.get`` are replaced for the duration of
the run with a tripwire that records the attempt instead of making it, so "no
model was called" is verified rather than assumed. The originals are restored in
a ``finally``.

Run:  python verify_analyst.py    Exit: 0 = all passed, 1 = at least one failed.
"""

from __future__ import annotations

import sys

import hydro_engine as H
import nugen_client as N

RESULTS: list[tuple[str, bool, str]] = []

# Any attempt to reach the network during this run. Must stay empty.
NETWORK: list[str] = []

SCENARIOS = ("normal", "flash_flood", "flash_drought", "dipole")
# Five ticks across the 0..32 window: the calm opening, the rise, the peak and
# the recession. A field that is present only at the peak (overtopping depth) or
# only in the calm (the "stays within the levee" clause) is exercised either way.
TICKS = (0, 6, 12, 20, 32)

LEVEE_Q = "How long until the levee overtops, and how many households are exposed?"
PLACE = "Krishna Basin (Sangli)"

# A string that only ever exists as a fake credential. If it reaches a prompt,
# the prompt builder is reading something it must not read.
FAKE_KEY = "nugen-sk-TRIPWIRE-0000-DO-NOT-SEND"

_STATES: dict[tuple[str, int], tuple] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def _safe(text: str) -> str:
    """ASCII-escape a detail line.

    The hostile-input cases carry emoji, and a Windows console is cp1252: a
    verifier that dies with UnicodeEncodeError while printing the name of the
    check that failed reports nothing at all.
    """
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def state_for(scenario: str, tick: int) -> tuple:
    """``(state, directives)`` — memoised so determinism checks compare like
    with like. ``real_compute_ms`` is measured per run, so two ``simulate``
    calls with the same arguments are not the same state."""
    key = (scenario, tick)
    if key not in _STATES:
        s = H.simulate(scenario, tick)
        _STATES[key] = (s, H.make_directives(s))
    return _STATES[key]


def _stage(ans, name: str) -> tuple:
    for row in ans.stages or []:
        if row[0] == name:
            return row
    return ("", "", "")


def _fields(facts) -> list:
    return [f.field for f in facts]


# ---------------------------------------------------------------------------
# 1 — SCOPE REJECTION
# ---------------------------------------------------------------------------
def test_scope() -> None:
    s, d = state_for("flash_flood", 12)
    off_domain = (("onions", "what is the price of onions in Pune?"),
                  ("match", "who won the match?"),
                  ("capital", "what is the capital of France?"))

    for tag, q in off_domain:
        ans = N.ask(s, d, q, PLACE, use_model=False)
        check(f"1a {tag}: declined as out of scope", ans.in_scope is False,
              f"in_scope={ans.in_scope}, topics={ans.topics}")
        # The grounded answer is the thing the UI prints as the answer. Empty is
        # the only honest value for a question the console cannot answer.
        check(f"1b {tag}: no grounded answer is composed", ans.grounded == "",
              _safe(ans.grounded[:120]))
        name, outcome, _detail = _stage(ans, "Scope check")
        check(f"1c {tag}: the harness row says out of scope",
              name == "Scope check" and outcome == "out of scope",
              f"{name!r}/{outcome!r}")
        check(f"1d {tag}: the model was not called",
              _stage(ans, "Language model")[1] == "not called" and not NETWORK,
              f"{_stage(ans, 'Language model')[1]!r}, network={NETWORK}")
        # The Retrieval row asserts "0 fields ... there was nothing to read".
        # ans.facts is what the UI renders as the evidence table, so the two must
        # agree — a panel that says nothing was read beside a table of readings
        # discredits the whole harness.
        check(f"1e {tag}: nothing was retrieved, as the harness claims",
              _stage(ans, "Retrieval")[1] == "0 fields" and ans.facts == [],
              f"Retrieval row says {_stage(ans, 'Retrieval')[1]!r} but "
              f"ans.facts carries {_fields(ans.facts)}")

    # A question with no topics must not be rescued by a word that happens to
    # appear in an unrelated field label.
    check("1f off-domain questions match no topic",
          all(N._topics_for(q) == [] for _tag, q in off_domain),
          str([N._topics_for(q) for _tag, q in off_domain]))

    # An off-domain request phrased as a request for advice: "what should I ..."
    # is an _ACTION_WORDS hit, and an action hit alone retrieves the three
    # standing orders. Scope must be decided by what the console HOLDS, not by
    # the grammatical mood of the question.
    for q in ("what should i cook for dinner?",
              "advise me on my stock portfolio",
              "recommend a good biryani place"):
        ans = N.ask(s, d, q, PLACE, use_model=False)
        check(f"1g off-domain advice is declined: {q[:22]!r}",
              ans.in_scope is False,
              f"topics={ans.topics}, answered with "
              f"{_safe(ans.grounded[:90])!r}")


# ---------------------------------------------------------------------------
# 2 — RELEVANCE RANKING  (the regression that started this)
# ---------------------------------------------------------------------------
def test_ranking() -> None:
    """Presence is not enough: the field asked about must survive the cut.

    ``MAX_FACTS`` truncates and ``_grounded_answer`` splices only the first four
    clauses, so a fact ranked 11th is retrieved and still never read. Both
    halves of the question are checked for position, not membership.
    """
    for scenario in SCENARIOS:
        for tick in TICKS:
            s, d = state_for(scenario, tick)
            ans = N.ask(s, d, LEVEE_Q, PLACE, use_model=False)
            fields = _fields(ans.facts)
            where = {f: i for i, f in enumerate(fields)}
            tag = f"{scenario}:{tick}"

            check(f"2a {tag}: both asked-for fields are retrieved",
                  "time_to_overtop_min" in where and "households_at_risk" in where,
                  str(fields))
            # First four = the window the grounded sentence actually uses.
            check(f"2b {tag}: both are ranked into the first four",
                  where.get("time_to_overtop_min", 99) < 4
                  and where.get("households_at_risk", 99) < 4,
                  f"tto={where.get('time_to_overtop_min')}, "
                  f"households={where.get('households_at_risk')} in {fields}")
            # The end the ranking exists for: they reach the operator's sentence.
            tto = next((f for f in ans.facts if f.field == "time_to_overtop_min"), None)
            hh = next((f for f in ans.facts if f.field == "households_at_risk"), None)
            check(f"2c {tag}: both reach the grounded sentence",
                  bool(tto and hh) and tto.clause in ans.grounded
                  and hh.clause in ans.grounded,
                  _safe(ans.grounded))
            # Scores must be strictly ordered, or the cut is arbitrary.
            scores = [f.score for f in ans.facts]
            check(f"2d {tag}: facts are sorted by score, high first",
                  scores == sorted(scores, reverse=True), str(scores))


# ---------------------------------------------------------------------------
# 3 — "irrigate" MUST NOT MATCH "gate"
# ---------------------------------------------------------------------------
def test_gate_substring() -> None:
    farmer_q = "Should farmers irrigate this week?"
    topics = N._topics_for(farmer_q)
    check("3a 'irrigate' does not trigger the release topic",
          "release" not in topics, str(topics))
    check("3b 'irrigate' does not trigger the reservoir topic",
          "reservoir" not in topics, str(topics))
    check("3c the farmer question still reaches the crop topics",
          {"crop", "soil", "drought"} <= set(topics), str(topics))

    # Word boundaries must not be bought at the price of the real match: the
    # gate question is the one question that SHOULD reach the release fields.
    gate_topics = N._topics_for("What should the duty engineer do with the "
                                "gates right now?")
    check("3d 'gates' still matches the release topic",
          "release" in gate_topics, str(gate_topics))

    # And the consequence, end to end: no reservoir-release reading may appear in
    # the evidence for a farmer's question.
    release_only = {"release_now", "firo_release", "spilling", "buffer_now_aft",
                    "reservoir_pct", "reservoir_level", "storage_aft"}
    for scenario in SCENARIOS:
        s, d = state_for(scenario, 12)
        got = set(_fields(N.retrieve(s, d, farmer_q)[0]))
        check(f"3e {scenario}: farmer question retrieves no release fields",
              not (got & release_only), str(sorted(got & release_only)))

    # Other stems that embed a topic word as a substring.
    check("3f 'how long until harvest' does not match 'arvest'-style substrings",
          "release" not in N._topics_for("when is the harvest?"),
          str(N._topics_for("when is the harvest?")))


# ---------------------------------------------------------------------------
# 4 / 5 / 6 — THE GROUNDED ANSWER, UNAIDED
# ---------------------------------------------------------------------------
def test_grounded_without_model() -> None:
    """The answer of record must exist for every suggested question, everywhere.

    These are the six questions the UI offers as buttons, so a blank answer is
    not an edge case — it is the demo failing in front of a judge. Checked with
    ``use_model=False``, which is the switched-off language layer.
    """
    blank, called, over_cap, invented = [], [], [], []

    for scenario in SCENARIOS:
        for tick in TICKS:
            s, d = state_for(scenario, tick)
            for q in N.SUGGESTED_QUESTIONS:
                tag = f"{scenario}:{tick}:{q[:18]}"
                ans = N.ask(s, d, q, PLACE, use_model=False)
                if not ans.in_scope or not ans.grounded.strip():
                    blank.append(tag)
                if _stage(ans, "Language model")[1] != "not called" or ans.result:
                    called.append(tag)
                if len(ans.facts) > N.MAX_FACTS:
                    over_cap.append(f"{tag}={len(ans.facts)}")
                # 5 — the rule-built answer can only quote what was retrieved,
                # by construction. A hit here means the composer invented a
                # figure, which is the one thing this layer promises it cannot do.
                bad = N.unsupported_numbers(ans.grounded, ans.digest)
                if bad:
                    invented.append(f"{tag}: {bad}")

    total = len(SCENARIOS) * len(TICKS) * len(N.SUGGESTED_QUESTIONS)
    check(f"4a every suggested question answers in all {total} states",
          not blank, _safe(f"{len(blank)} blank: {blank[:4]}"))
    check("4b no stage reports a model call", not called,
          _safe(str(called[:4])))
    check("5a no figure in a grounded answer is absent from the digest",
          not invented, _safe(str(invented[:4])))
    check(f"6a fact count never exceeds MAX_FACTS ({N.MAX_FACTS})",
          not over_cap, str(over_cap[:6]))

    # The cap has to bite somewhere, or it is untested scaffolding.
    s, d = state_for("dipole", 12)
    broad = N.retrieve(s, d, "what should we do about the flood, the reservoir, "
                              "the release, the rain, the levee, the soil, the "
                              "crop, the weather and the people?")[0]
    check("6b a question touching every topic is truncated to the cap",
          len(broad) == N.MAX_FACTS, str(len(broad)))
    check("6c the truncation keeps the highest-scoring facts",
          broad[0].score >= broad[-1].score,
          f"{broad[0].score} -> {broad[-1].score}")


# ---------------------------------------------------------------------------
# 7 — THE PROMPT HOLDS THE QUESTION AND THE FACTS, AND NOTHING ELSE
# ---------------------------------------------------------------------------
def test_prompt_surface() -> None:
    all_labels = {lbl for _a, lbl, _g, _t, _tpl in N._FIELDS}

    for scenario in SCENARIOS:
        s, d = state_for(scenario, 12)
        for q in N.SUGGESTED_QUESTIONS[:4]:
            tag = f"{scenario}:{q[:18]}"
            facts, _topics = N.retrieve(s, d, q)
            digest = N.build_analyst_digest(facts, q, PLACE,
                                            getattr(s, "clock", ""))

            check(f"7a {tag}: every retrieved fact is in the prompt",
                  all(f"{f.label}: {f.value}" in digest for f in facts),
                  _safe(str([f.label for f in facts
                             if f"{f.label}: {f.value}" not in digest])))
            check(f"7b {tag}: the prompt carries the question",
                  f"QUESTION: {q}" in digest, _safe(digest.splitlines()[0]))
            # A field that was not retrieved must not be visible to the model:
            # the whole retrieval surface is _FIELDS, so the labels it did not
            # select are exactly what must be absent.
            leaked = sorted(lbl for lbl in all_labels - {f.label for f in facts}
                            if lbl in digest)
            check(f"7c {tag}: no unselected state field leaks in", not leaked,
                  str(leaked))
            # Bounded input tokens: complete() hard-truncates at this limit, so a
            # digest over it is a prompt whose tail was silently cut.
            check(f"7d {tag}: digest stays inside _PROMPT_CHAR_LIMIT",
                  len(digest) < N._PROMPT_CHAR_LIMIT,
                  f"{len(digest)} >= {N._PROMPT_CHAR_LIMIT}")

    # The preview is the "nothing is hidden" panel: it must be the system rule,
    # the fixed example, and this question's digest — with no fourth thing.
    s, d = state_for("flash_flood", 12)
    preview = N.analyst_preview(s, d, LEVEE_Q, PLACE)
    facts, _ = N.retrieve(s, d, LEVEE_Q)
    digest = N.build_analyst_digest(facts, LEVEE_Q, PLACE,
                                    getattr(s, "clock", ""))
    residue = preview
    for block in (N.ANALYST_SYSTEM, N._ANALYST_EXAMPLE_Q, N._ANALYST_EXAMPLE_A,
                  digest, "[system]", "[user]", "[assistant]"):
        residue = residue.replace(block, "")
    check("7e the preview is the rule, the example and this digest only",
          residue.strip() == "", _safe(residue.strip()[:200]))
    check("7f the preview shows the digest verbatim", digest in preview)

    # A credential is not a fact. It is passed to ask() and must not survive
    # anywhere near the payload.
    ans = N.ask(s, d, LEVEE_Q, PLACE, api_key=FAKE_KEY, use_model=False)
    blob = ans.digest + ans.grounded + repr(ans.stages) + preview
    check("7g the API key never reaches the prompt or the harness",
          FAKE_KEY not in blob and "NUGEN_API_KEY" not in ans.digest,
          _safe(blob[max(0, blob.find(FAKE_KEY) - 40):][:120]))


# ---------------------------------------------------------------------------
# 8 — HOSTILE INPUT  (``ask`` is documented never to raise)
# ---------------------------------------------------------------------------
def test_hostile_input() -> None:
    s, d = state_for("dipole", 16)
    hostile = [
        ("empty", ""),
        ("whitespace", "   \t\n  "),
        ("5000 chars", "x" * 5000),
        ("long real question", LEVEE_Q + " " + "padding " * 700),
        ("brace placeholders", "levee {} {v} {l} {0} {label} overtop"),
        ("unbalanced brace", "how long until the levee {"),
        ("format spec", "flood {0:>99999999} {v!r}"),
        ("html", "<script>alert('flood')</script><b>levee</b>"),
        ("emoji", "flood \U0001F30A how long \U0001F6A8 levee"),
        ("none", None),
        ("int", 42),
        ("float", 3.5),
        ("list", ["levee", "flood"]),
    ]

    for name, q in hostile:
        # Every entry point the UI can reach with raw text, including the
        # api_key='' fallback branch, which runs more code than use_model=False.
        raised, broken = [], []
        answers = []
        for label, fn in (
                ("_topics_for", lambda: N._topics_for(q)),
                ("retrieve", lambda: N.retrieve(s, d, q)),
                ("build_analyst_digest",
                 lambda: N.build_analyst_digest(N.retrieve(s, d, q)[0], q, PLACE)),
                ("analyst_preview", lambda: N.analyst_preview(s, d, q, PLACE)),
                ("ask/no-model", lambda: N.ask(s, d, q, PLACE, use_model=False)),
                ("ask/fallback",
                 lambda: N.ask(s, d, q, PLACE, api_key="", use_model=True))):
            try:
                out = fn()
            except Exception as exc:
                raised.append(f"{label}: {type(exc).__name__}: {exc}")
                continue
            if label.startswith("ask"):
                answers.append((label, out))
        check(f"8a {name}: no entry point raises", not raised,
              _safe("; ".join(raised)))
        # Never raising is worth nothing if it returns a broken object: the UI
        # indexes stages and iterates facts without guarding either.
        for label, out in answers:
            if not (isinstance(out, N.Answer) and isinstance(out.facts, list)
                    and isinstance(out.grounded, str)
                    and isinstance(out.stages, list) and len(out.stages) == 6):
                broken.append(f"{label}: stages={len(getattr(out, 'stages', []))}")
        check(f"8b {name}: a usable six-stage Answer comes back", not broken,
              _safe("; ".join(broken)))

    # The one hostile case with a real consequence: the question is placed ahead
    # of the facts in the digest and is not itself bounded, so a question longer
    # than _PROMPT_CHAR_LIMIT evicts the entire FACTS block from the prompt that
    # complete() actually sends — while the system rule still orders the model to
    # use ONLY the FACTS given.
    long_q = LEVEE_Q + " " + "padding " * 700
    facts, _ = N.retrieve(s, d, long_q)
    sent = N.build_analyst_digest(facts, long_q, PLACE)[:N._PROMPT_CHAR_LIMIT]
    check("8c a 5000-char question does not evict the facts from the prompt",
          "FACTS:" in sent and any(f.label in sent for f in facts),
          f"{len(facts)} facts retrieved, {sum(1 for f in facts if f.label in sent)} "
          f"survive the {N._PROMPT_CHAR_LIMIT}-char cut applied in complete()")


# ---------------------------------------------------------------------------
# 9 — FALLBACK: NO KEY IS A HANDLED STATE, NOT AN OUTAGE
# ---------------------------------------------------------------------------
def test_fallback() -> None:
    for scenario in SCENARIOS:
        s, d = state_for(scenario, 12)
        ans = N.ask(s, d, LEVEE_Q, PLACE, api_key="", use_model=True)
        tag = scenario

        check(f"9a {tag}: the grounded answer stands without a key",
              bool(ans.grounded.strip()), _safe(ans.grounded[:80]))
        # verified gates whether the model's wording may be shown at all.
        check(f"9b {tag}: the answer is not marked verified",
              ans.verified is False,
              f"verified={ans.verified}, result={ans.result}")
        check(f"9c {tag}: the harness reports the model unavailable",
              _stage(ans, "Language model")[1] == "unavailable",
              repr(_stage(ans, "Language model")[1]))
        check(f"9d {tag}: the harness reports the fallback in use",
              _stage(ans, "Fallback")[1] == "in use",
              repr(_stage(ans, "Fallback")[1]))
        check(f"9e {tag}: the reason given is the missing key",
              "No API key" in (ans.result.error if ans.result else ""),
              _safe((ans.result.error if ans.result else "no result")[:80]))
        check(f"9f {tag}: the figure check is not claimed to have passed",
              _stage(ans, "Figure check")[1] == "not reached",
              repr(_stage(ans, "Figure check")[1]))
        # The point of the whole exercise: an unconfigured key costs no call.
        check(f"9g {tag}: no network call was attempted", not NETWORK,
              str(NETWORK))


# ---------------------------------------------------------------------------
# 10 — DETERMINISM
# ---------------------------------------------------------------------------
def test_determinism() -> None:
    """Same state, same question, same fact sheet.

    An operator who asks twice and reads two different sheets cannot trust
    either. Retrieval is attribute lookup plus a stable sort, so this must hold
    exactly — fields, values and order.
    """
    questions = list(N.SUGGESTED_QUESTIONS) + [LEVEE_Q, "what should we do now?"]
    for scenario in SCENARIOS:
        s, d = state_for(scenario, 12)
        drifted = []
        for q in questions:
            a, ta = N.retrieve(s, d, q)
            b, tb = N.retrieve(s, d, q)
            sig_a = [(f.field, f.label, f.value, f.clause, f.score) for f in a]
            sig_b = [(f.field, f.label, f.value, f.clause, f.score) for f in b]
            if sig_a != sig_b or ta != tb:
                drifted.append(f"{q[:24]}: {sig_a[:2]} vs {sig_b[:2]}")
        check(f"10a {scenario}: retrieve is identical twice for all "
              f"{len(questions)} questions", not drifted,
              _safe("; ".join(drifted)[:200]))

    # And the whole pipeline on top of it, including the composed sentence.
    s, d = state_for("flash_flood", 12)
    first = N.ask(s, d, LEVEE_Q, PLACE, use_model=False)
    second = N.ask(s, d, LEVEE_Q, PLACE, use_model=False)
    check("10b ask is identical twice (grounded, digest, topics, stages)",
          (first.grounded, first.digest, first.topics, first.stages)
          == (second.grounded, second.digest, second.topics, second.stages),
          _safe(first.grounded[:60] + " | " + second.grounded[:60]))

    # Order must come from the score, not from the registry: the same question
    # phrased with its clauses swapped retrieves the same sheet.
    swapped = "How many households are exposed, and how long until the levee overtops?"
    check("10c clause order in the question does not change the sheet",
          _fields(N.retrieve(s, d, LEVEE_Q)[0])
          == _fields(N.retrieve(s, d, swapped)[0]),
          _safe(str(_fields(N.retrieve(s, d, swapped)[0]))))


# ---------------------------------------------------------------------------
# The tripwire: nothing in this file may touch the network.
# ---------------------------------------------------------------------------
def _tripwire(*_args, **_kwargs):
    NETWORK.append("HTTP call attempted")
    raise RuntimeError("verify_analyst tripwire: no network calls allowed")


def main() -> int:
    saved_post, saved_get = N.requests.post, N.requests.get
    N.requests.post = _tripwire
    N.requests.get = _tripwire
    try:
        for fn in (test_scope, test_ranking, test_gate_substring,
                   test_grounded_without_model, test_prompt_surface,
                   test_hostile_input, test_fallback, test_determinism):
            try:
                fn()
            except Exception as exc:                    # a raise is a failure
                check(f"{fn.__name__} raised", False,
                      _safe(f"{type(exc).__name__}: {exc}"))
    finally:
        N.requests.post = saved_post
        N.requests.get = saved_get

    check("0a the endpoint was never contacted", not NETWORK, str(NETWORK[:3]))
    check("0b no completion budget was spent", N._calls_made == 0,
          f"_calls_made={N._calls_made}")

    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + (f"   [{_safe(detail)}]" if detail and not ok else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed "
          f"({len(failed)} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
