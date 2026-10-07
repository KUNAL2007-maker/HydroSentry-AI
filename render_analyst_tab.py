"""Render the AI analyst tab's real HTML to a standalone file for visual review.

The browser preview tool is unavailable, so this is the honest substitute: drive
app.py through Streamlit's own AppTest harness exactly as verify_app.py does,
collect the markdown blocks the AI analyst tab actually emitted, and wrap them in
the app's real stylesheet. What comes out is the same HTML the browser would be
handed, so a person can open it and look at it — and the structural checks below
run on the same bytes.

Deliberately not a test: it asserts nothing about appearance. It produces the
artefact and reports the structural facts a machine can establish.
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["HYDRO_BROWSER_LIVE"] = "0"
os.environ.pop("NUGEN_API_KEY", None)
os.environ.pop("HYDRO_NUGEN_KEY", None)

from streamlit.testing.v1 import AppTest     # noqa: E402

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
OUT = "analyst_tab_render.html"


def read_css() -> str:
    """Lift the CSS literal out of app.py WITHOUT executing it.

    ``import app`` looks like the obvious way to get at ``app.CSS`` and is a
    trap: importing runs the whole script in bare Streamlit mode, which enters
    the sidebar's ``st.form("place_search")`` and never cleanly leaves it. The
    next AppTest run in the same process then fails on the mode radio with
    "within a form, callbacks can only be defined on st.form_submit_button" —
    an error in this harness that reads exactly like an error in the app.
    """
    tree = ast.parse(io.open(APP, encoding="utf-8").read())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "CSS" for t in node.targets)
                and isinstance(node.value, ast.Constant)):
            return str(node.value.value)
    raise SystemExit("could not find the CSS literal in app.py")


# Tags whose balance is worth counting. Void elements are excluded.
TAGS = ("div", "span", "table", "thead", "tbody", "tr", "td", "th",
        "ul", "ol", "li", "p", "code", "b", "i", "h2", "h3")


def bodies(at) -> list:
    """Every markdown/caption body on the page, in render order."""
    out = []
    for coll in (at.markdown, at.caption):
        for el in coll:
            try:
                out.append(str(el.value))
            except Exception:
                pass
    return out


def analyst_blocks(all_bodies: list) -> list:
    """The blocks belonging to the AI analyst tab.

    Identified by the copy that only that tab emits, from its heading to the
    closing comparison caption. Crude but exact: the markers are literal strings
    written in render_analyst and _render_comparison.
    """
    start = next((i for i, b in enumerate(all_bodies)
                  if "AI support" in b), None)
    if start is None:
        return []
    end = next((i for i, b in enumerate(all_bodies)
                if "the decision a control room needs does not require one" in b),
               len(all_bodies) - 1)
    return all_bodies[start:end + 1]


def main() -> int:
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()

    # Live is the default and the feed may be unreachable from here; the demo is
    # deterministic and is what a reviewer is shown anyway.
    try:
        at.radio(key="mode").set_value("demo").run()
    except Exception as exc:
        print("could not switch to demo mode:", exc)

    if at.exception:
        print("FIRST RENDER RAISED:")
        for e in at.exception:
            print(" ", e.value)
        return 1

    # Ask something, so the answer card, the evidence table and the harness are
    # all on the page rather than the empty-state line.
    chip = next((k for k in (w.key for w in at.button)
                 if k and k.startswith("ask_chip_")), None)
    if chip:
        at.button(key=chip).click().run()
    if at.exception:
        print("RENDER AFTER CHIP CLICK RAISED:")
        for e in at.exception:
            print(" ", e.value)
        return 1

    css = read_css()
    all_bodies = bodies(at)
    blocks = analyst_blocks(all_bodies)
    print(f"collected {len(blocks)} blocks from the AI analyst tab "
          f"(of {len(all_bodies)} on the page)")

    html = "\n".join(blocks)

    # ---- structural checks on the exact bytes a browser would get -----------
    problems = []

    # 1. A visible "&amp;" means something was escaped twice.
    for n, b in enumerate(blocks):
        if "&amp;amp;" in b:
            problems.append(f"block {n}: double-escaped ampersand (&amp;amp;)")

    # 2. Tag balance, per block — an unclosed div swallows the rest of the tab.
    for n, b in enumerate(blocks):
        for tag in TAGS:
            opens = len(re.findall(r"<%s(?=[\s>])" % tag, b))
            closes = len(re.findall(r"</%s>" % tag, b))
            if opens != closes:
                problems.append(
                    f"block {n}: <{tag}> opened {opens}x, closed {closes}x")

    # 3. Every class used must exist in the stylesheet.
    used = set(re.findall(r'class="([^"]+)"', html))
    classes = {c for group in used for c in group.split()}
    missing = sorted(c for c in classes
                     if c.startswith("hs-") and ("." + c) not in css)
    for c in missing:
        problems.append(f"class {c!r} used but not defined in CSS")

    # 4. Raw angle brackets from data would show as markup; _esc should have
    #    turned them into entities. A stray "<" not opening a known tag is the
    #    signature.
    stray = re.findall(r"<(?![/!]?[a-zA-Z])", html)
    if stray:
        problems.append(f"{len(stray)} stray '<' not starting a tag")

    print()
    if problems:
        print("STRUCTURAL PROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  -", p)
    else:
        print("structural checks clean: tags balanced, no double-escaping, "
              "every hs- class defined, no stray '<'")

    print()
    print("classes used:", ", ".join(sorted(classes)))
    tag_counts = Counter(re.findall(r"<([a-zA-Z0-9]+)(?=[\s>])", html))
    print("elements:", dict(tag_counts.most_common()))

    # ---- the artefact ------------------------------------------------------
    page = (
        "<!doctype html>\n<html><head><meta charset='utf-8'>\n"
        "<title>HydroSentry-AI — AI analyst tab (static render)</title>\n"
        "<style>" + css + "</style>\n"
        "<style>body{background:var(--paper);color:var(--ink);"
        "font-family:'IBM Plex Sans',system-ui,sans-serif;margin:0;"
        "padding:28px 32px;} .wrap{max-width:1100px;margin:0 auto;} "
        ".snapshot-note{background:#FFF8E1;border:1px solid #F0E0A8;"
        "border-radius:12px;padding:12px 16px;margin:0 0 22px;font-size:13px;"
        "color:#6B5A14;line-height:1.55;}</style>\n"
        "</head><body><div class='wrap'>\n"
        "<div class='snapshot-note'><b>Static snapshot.</b> This is the real "
        "HTML the AI analyst tab emitted through Streamlit's own test harness, "
        "wrapped in the app's real stylesheet, so the cards, tables and badges "
        "render exactly as they do in the app. What is <i>not</i> here: the "
        "Streamlit chrome (tab bar, buttons, text box, expander), the charts, "
        "and the answer card's model-worded variant, which needs an API key. "
        "The demo scenario is the one the app starts on.</div>\n"
        + html +
        "\n</div></body></html>\n")
    io.open(OUT, "w", encoding="utf-8").write(page)
    print()
    print("wrote", OUT, f"({len(page):,} bytes)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
