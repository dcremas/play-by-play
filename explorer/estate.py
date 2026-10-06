"""The cross-site footer every surface on dustincremascoli.com ends with.

Same row, same order, on the two Flask sites, the Bokeh apps, the Swagger docs,
the analytics dashboard, the weather SQL Explorer, PromptPace, the Dash play
explorer under /plays/ and this app. The canonical copy is SITES in
~/projects/ec2-nginx/prosite_flask/content.py; ec2-nginx/check-footer-nav.sh
diffs the copies inside that directory, and this one (outside it) is kept in step
by hand. The markup and styling follow PromptPace's footer (typing/app/static/), since
2026-10-06 when this app took PromptPace's look; the site list and order are unchanged.

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

# NOTHING TAG-SHAPED MAY APPEAR IN THIS CSS, comments included. st.html runs DOMPurify,
# which deletes a <style> whose text contains "<" followed by a letter (its mXSS guard), so
# one comment naming an SVG element rendered the whole footer as a bare bulleted list.
#
# PromptPace's footer, class for class (typing/app/static/styles.css, .site-footer), so the
# row reads the same on both apps. The --pp-* colours are defined in style.py, which every
# page injects first.
_CSS = """
<style>
.ppx-footer { margin-top: 40px; border-top: 1px solid var(--pp-border); padding: 32px 0 56px; }
.ppx-notes { margin: 0 0 24px; max-width: 80ch; color: var(--pp-muted); font-size: .85rem; }
.ppx-notes a { color: var(--pp-text-2); font-weight: 550; }
.ppx-nav { padding-bottom: 24px; margin-bottom: 22px; border-bottom: 1px solid var(--pp-border); }
.ppx-label { margin: 0 0 8px; color: var(--pp-muted); font-size: .68rem; font-weight: 650;
  letter-spacing: .08em; text-transform: uppercase; }
.ppx-footer ul { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 22px;
  list-style: none; padding: 0; margin: 0; }
.ppx-footer li { display: flex; align-items: center; margin: 0; padding: 0; }
.ppx-link { display: inline-flex; align-items: center; gap: 6px; padding-block: 2px;
  color: var(--pp-text-2) !important; text-decoration: none !important; font-size: .9rem;
  font-weight: 550; }
.ppx-link:hover { color: var(--pp-accent) !important; }
/* PromptPace's external-link glyph, as a mask: st.html strips inline SVG markup. */
.ppx-nav .ppx-link[target]::after { content: ""; width: 1em; height: 1em; opacity: .55;
  background: currentColor; -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M14 4h6v6M20 4l-8.5 8.5'/%3E%3Cpath d='M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4'/%3E%3C/svg%3E") center / contain no-repeat;
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.9' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M14 4h6v6M20 4l-8.5 8.5'/%3E%3Cpath d='M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4'/%3E%3C/svg%3E") center / contain no-repeat; }
.ppx-nav .ppx-link[target]:hover::after { opacity: 1; }
.ppx-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--pp-live);
  box-shadow: 0 0 0 2px var(--pp-live-halo); }
.ppx-link[aria-current] { color: var(--pp-muted) !important; font-weight: 650; }
.ppx-link[aria-current] .ppx-dot { background: var(--pp-muted); box-shadow: none; }
.ppx-meta { margin: 0; color: var(--pp-muted); font-size: .85rem; line-height: 1.7; }
.ppx-meta .ppx-link { font-size: .875rem; }
.ppx-meta .ppx-sep { margin-inline: 7px; }
.ppx-footer a:focus-visible { outline: 2px solid var(--pp-accent); outline-offset: 2px;
  border-radius: 4px; }
</style>
"""

def _row(sites, current):
    items = []
    for key, label, href in sites:
        if key == current:
            items.append(f'<li><a class="ppx-link" href="{href}" aria-current="page">'
                         f'<span class="ppx-dot" aria-hidden="true"></span>{html.escape(label)}</a></li>')
        else:
            items.append(f'<li><a class="ppx-link" href="{href}" target="_blank" '
                         f'rel="noopener noreferrer"><span class="ppx-dot" aria-hidden="true"></span>'
                         f'{html.escape(label)}</a></li>')
    return "".join(items)


def footer(notes: str = "") -> None:
    """Render the footer. Call once, last, from the main body.

    `notes` is trusted markup for the small print above the site row -- PromptPace puts its
    definitions there, and this app puts its source credit and its one caveat.
    """
    socials = '<span class="ppx-sep" aria-hidden="true">·</span>'.join(
        f'<a class="ppx-link" href="{h}" target="_blank" rel="noopener noreferrer">'
        f"{html.escape(n)}</a>"
        for n, h in SOCIALS
    )
    st.html(
        _CSS
        + '<footer class="ppx-footer">'
        + (f'<p class="ppx-notes">{notes}</p>' if notes else "")
        + '<nav class="ppx-nav" aria-label="Site">'
        + f'<p class="ppx-label">{SITES_LABEL}</p>'
        + f"<ul>{_row(SITES, SITES_CURRENT)}</ul></nav>"
        + f'<p class="ppx-meta">© {datetime.now(timezone.utc).year} Dustin Cremascoli. '
        + "All rights reserved."
        + '<span class="ppx-sep" aria-hidden="true">·</span>'
        + f"{html.escape(BUILT_WITH)}"
        + f'<span class="ppx-sep" aria-hidden="true">·</span>{CREDIT}<br>{socials}</p>'
        + "</footer>"
    )
