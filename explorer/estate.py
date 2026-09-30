"""The cross-site footer every surface on dustincremascoli.com ends with.

Same row, same order, on the two Flask sites, the Bokeh apps, the Swagger docs,
the analytics dashboard, the weather SQL Explorer, PromptPace, the Dash play
explorer under /plays/ and this app. The canonical copy is SITES in
~/projects/ec2-nginx/prosite_flask/content.py; ec2-nginx/check-footer-nav.sh
diffs the copies inside that directory, and this one (outside it) is kept in step
by hand. The markup and styling follow weather-sql-explorer/sql_explorer/ui.py,
the other Streamlit copy.

Every href is absolute, including this app's own: a Streamlit app is a single
page and a relative "/" would reload it rather than go anywhere.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

import streamlit as st

SITES = [
    ("www", "Main site", "https://www.dustincremascoli.com/"),
    ("viz", "Data Viz", "https://www.dustincremascoli.com/visualizations"),
    ("sql", "SQL Explorer", "https://sql.dustincremascoli.com/"),
    ("api", "Weather API", "https://api.dustincremascoli.com/docs"),
    ("pbp", "Football SQL", "https://pbp.dustincremascoli.com/"),
    ("plays", "Play Explorer", "https://pbp.dustincremascoli.com/plays/"),
    ("typing", "PromptPace", "https://typing.dustincremascoli.com/"),
    ("recipes", "Recipes", "https://recipes.dustincremascoli.com/"),
]

SITES_CURRENT = "pbp"
SITES_LABEL = "Everything here"
BUILT_WITH = "Built with Python, Streamlit and Gemini — self-hosted on AWS EC2 behind nginx."
CREDIT = "Produced with the aid of Claude Code&#39;s Command Line tools..."
SOCIALS = [
    ("GitHub", "https://github.com/dcremas"),
    ("LinkedIn", "https://www.linkedin.com/in/dustin-cremascoli-662105423/"),
]

# Grey via rgba so it reads on Streamlit's light and dark themes alike; the
# color-mix block tightens it where supported, as the weather explorer does.
_CSS = """
<style>
.ppx-footer { margin: 3rem 0 .5rem 0; padding-top: 1.2rem;
  border-top: 1px solid rgba(128,128,128,.28); }
.ppx-footer .ppx-label { margin: 0 0 .4rem 0; font-size: .68rem; font-weight: 650;
  letter-spacing: .08em; text-transform: uppercase; color: rgba(128,128,128,.95); }
.ppx-footer ul { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap;
  gap: .2rem .3rem; font-size: .9rem; }
.ppx-footer li { display: inline-flex; align-items: center; }
.ppx-footer a { color: #3b82f6; text-decoration: none; font-weight: 550; }
.ppx-footer a:hover { text-decoration: underline; }
.ppx-footer a[aria-current] { color: rgba(128,128,128,.95); font-weight: 650; }
.ppx-footer .ppx-sep { color: rgba(128,128,128,.5); margin: 0 .4rem; }
.ppx-footer .ppx-meta { margin: .9rem 0 0 0; font-size: .78rem; color: rgba(128,128,128,.95); }
.ppx-footer .ppx-meta a { font-weight: 550; }
@supports (color: color-mix(in srgb, red 50%, blue)) {
  .ppx-footer { border-top-color: color-mix(in srgb, currentColor 18%, transparent); }
  .ppx-footer .ppx-label, .ppx-footer .ppx-meta, .ppx-footer a[aria-current] {
    color: color-mix(in srgb, currentColor 58%, transparent); }
  .ppx-footer .ppx-sep { color: color-mix(in srgb, currentColor 30%, transparent); }
}
</style>
"""


def _row(sites, current):
    items = []
    for i, (key, label, href) in enumerate(sites):
        here = key == current
        mark = ' aria-current="page"' if here else ""
        # The separator lives inside the <li>: a bare <span> in a <ul> is invalid
        # and gets hoisted out of the list.
        sep = "" if i == len(sites) - 1 else '<span class="ppx-sep" aria-hidden="true">·</span>'
        tab = "" if here else ' target="_blank" rel="noopener noreferrer"'
        items.append(f'<li><a href="{href}"{mark}{tab}>{html.escape(label)}</a>{sep}</li>')
    return "".join(items)


def footer() -> None:
    """Render the footer. Call once, last, from the main body."""
    socials = " · ".join(
        f'<a href="{h}" target="_blank" rel="noopener noreferrer">{html.escape(n)}</a>'
        for n, h in SOCIALS
    )
    st.html(
        _CSS
        + '<div class="ppx-footer"><nav aria-label="Site">'
        + f'<p class="ppx-label">{SITES_LABEL}</p>'
        + f"<ul>{_row(SITES, SITES_CURRENT)}</ul></nav>"
        + f'<p class="ppx-meta">© {datetime.now(timezone.utc).year} Dustin Cremascoli. '
        + "All rights reserved. · "
        + f"{html.escape(BUILT_WITH)} · {CREDIT}<br>{socials}</p></div>"
    )
