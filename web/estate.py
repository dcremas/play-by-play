"""The cross-site footer every surface on dustincremascoli.com ends with.

Same row, same order, everywhere nginx serves on the box. The canonical copy is
SITES in ~/projects/ec2-nginx/prosite_flask/content.py; ec2-nginx/check-footer-nav.sh
diffs the copies inside that directory, and this one (outside it) is kept in step
by hand, as is its twin in explorer/estate.py.

Hrefs are absolute, this app's own included, like the other copies: the app is
mounted under a prefix (PBP_WEB_PREFIX), so a relative href would depend on it.
This app's own entry renders as a span, not a link -- the explorer's filters live
in memory, so a full-page load of /plays/ would reset them -- but its href stays
in SITES for check-footer-nav.sh. Every link out opens a new tab and says so: a
visible arrow plus screen-reader text (link policy sections 2a, 3 and 4).
"""

from __future__ import annotations

from datetime import datetime, timezone

from dash import html

SITES = [
    ("www", "Main site", "https://www.dustincremascoli.com/"),
    ("viz", "Data Viz", "https://www.dustincremascoli.com/visualizations"),
    ("sql", "Ask Weather", "https://sql.dustincremascoli.com/"),
    ("api", "Weather API", "https://api.dustincremascoli.com/docs"),
    ("pbp", "Ask Football", "https://pbp.dustincremascoli.com/"),
    ("plays", "Play Explorer", "https://pbp.dustincremascoli.com/plays/"),
    ("typing", "PromptPace", "https://typing.dustincremascoli.com/"),
    ("recipes", "Recipes", "https://recipes.dustincremascoli.com/"),
]

SITES_CURRENT = "plays"
SITES_LABEL = "Everything here"
BUILT_WITH = "Built with Python, Dash and DuckDB — self-hosted on AWS EC2 behind nginx."
CREDIT = "Produced with the help of Claude Code."
SOCIALS = [
    ("GitHub", "https://github.com/dcremas"),
    ("LinkedIn", "https://www.linkedin.com/in/dustin-cremascoli-662105423"),
]

# The estate's own sites get rel="noopener" alone so the browser still sends a
# Referer, which the weblog needs to classify cross-site traffic. Third parties
# (GitHub, LinkedIn, ESPN) also get noreferrer.
_NEW_TAB_OWN = {"target": "_blank", "rel": "noopener"}
_NEW_TAB = {"target": "_blank", "rel": "noopener noreferrer"}


def new_tab_cue() -> list:
    """The children that end every new-tab link's text: arrow, then the words."""
    return [
        html.Span("↗", className="st-ext", **{"aria-hidden": "true"}),
        html.Span(" (opens in a new tab)", className="sr-only"),
    ]


def _site(key: str, label: str, href: str, last: bool):
    here = key == SITES_CURRENT
    link = (
        html.Span(label, className="st-estate-link is-current", **{"aria-current": "page"})
        if here
        else html.A([label, *new_tab_cue()], href=href, className="st-estate-link", **_NEW_TAB_OWN)
    )
    kids = [link] if last else [link, html.Span("·", className="st-estate-sep", **{"aria-hidden": "true"})]
    return html.Li(kids)


def footer_children() -> list:
    """The estate row and credit line, for the end of the page footer."""
    socials = []
    for i, (name, href) in enumerate(SOCIALS):
        if i:
            socials.append(" · ")
        socials.append(html.A([name, *new_tab_cue()], href=href, **_NEW_TAB))
    return [
        html.Nav(className="st-estate", **{"aria-label": "Site"}, children=[
            html.P(SITES_LABEL, className="st-estate-label"),
            html.Ul([_site(k, lbl, h, i == len(SITES) - 1) for i, (k, lbl, h) in enumerate(SITES)]),
        ]),
        html.P(className="st-estate-meta", children=[
            (
                f"© {datetime.now(timezone.utc).year} Dustin Cremascoli. All rights reserved. · "
                f"{BUILT_WITH} · {CREDIT} "
            ),
            html.Br(),
            *socials,
        ]),
    ]
