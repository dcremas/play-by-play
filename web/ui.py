"""Shared UI primitives: stat tiles, section headers, grid factory, caveat notes."""
from __future__ import annotations

import os

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import dcc, html

from . import theme

BLOCK = 120  # infinite-scroll block size

# The work-in-progress notice, and the switch that decides whether there is one.
#
# OFF unless PBP_WEB_WIP is set, so a local run is never cluttered by it. Set it
# to `1` for the default wording below, or to any other non-empty string to use
# that string as the message -- which is the useful form: it means a change of
# wording is a systemd drop-in and a restart, not an edit and a redeploy.
#
# It is a NOTICE, not a gate. Everything on the page works; the banner exists so
# that someone handed this link knows which numbers are settled and which are not
# before they quote one, and so that the answer is on the page rather than in the
# message that shared it.
#
# The wording does NOT repeat the title. `dmc.Alert` renders `title` above the
# body, so a message opening "Work in progress —" reads as the phrase twice.
_WIP_DEFAULT = (
    "This explorer is still being built, and the 2026 season is partial in both "
    "leagues. Check a number here before you quote it elsewhere."
)


def wip_banner():
    """The WIP strip, or nothing at all when the switch is off.

    Deliberately NOT dismissible. `dmc.Alert` takes `withCloseButton`, but `hide`
    is an ordinary boolean prop rather than something the close button drives, so
    the button would need a callback behind it to do anything -- and a close
    button that does not close is a worse defect than no close button. It is also
    the wrong feature here: a caveat someone can dismiss on their first visit is a
    caveat they will not see on the visit where they take a number from the page.
    """
    raw = (os.environ.get("PBP_WEB_WIP") or "").strip()
    if not raw:
        return None
    return dmc.Alert(
        _WIP_DEFAULT if raw == "1" else raw,
        title="Work in progress",
        color="yellow",
        variant="light",
        id="wip-banner",
        mb="md",
    )


def tile(label: str, value, sub: str | None = None, tone: str | None = None):
    """A stat tile. Value in primary ink -- never in a series colour.

    `c` is omitted rather than passed as None: Dash serialises None to JSON null,
    and Mantine's colour parser sees `typeof null === "object"` and throws, which
    takes the whole tile row down with it.
    """
    value_kw = {"c": tone} if tone else {}
    return dmc.Paper(
        withBorder=True, radius="md", p="sm", className="tile",
        children=dmc.Stack(gap=2, children=[
            dmc.Text(label, size="xs", c="dimmed", className="tile-label"),
            dmc.Text(str(value), className="tile-value", **value_kw),
            dmc.Text(sub or "", size="xs", c="dimmed", className="tile-sub"),
        ]),
    )


def section(title: str, subtitle: str | None = None, right=None):
    return dmc.Group(justify="space-between", align="flex-end", mb="xs", children=[
        dmc.Stack(gap=0, children=[
            dmc.Text(title, fw=600, size="sm"),
            dmc.Text(subtitle or "", size="xs", c="dimmed"),
        ]),
        right or html.Div(),
    ])


def note(children, tone: str = "neutral"):
    """A caveat line. Always icon + text, never colour alone."""
    icon = {"neutral": "•", "warn": "!", "info": "i"}[tone]
    return dmc.Group(gap=6, align="flex-start", className=f"note note-{tone}", children=[
        dmc.Text(icon, size="xs", className="note-icon"),
        dmc.Text(children, size="xs", c="dimmed", style={"flex": 1}),
    ])


def graph(gid: str, height: int = 300):
    """A Plotly panel.

    `responsive` is doing less than it sounds like: plotly re-measures its container on
    a **window resize event** and on nothing else, so a chart whose container changes
    size any other way keeps a stale width. Every reveal and re-layout in this app is
    an "other way" -- see assets/resize.js, which is what actually keeps these honest.
    """
    return dcc.Graph(
        id=gid, config={"displayModeBar": False, "responsive": True},
        style={"height": f"{height}px"}, className="viz",
    )


def grid(gid: str, mode: str = "dark", *, infinite: bool = False,
         columns=None, rows=None, height: str = "560px", row_id: str | None = None,
         extra_options: dict | None = None):
    """AG Grid with this app's defaults.

    Infinite row model for the play grids: 200k rows never go to the browser, and
    sorting and per-column filtering are pushed down into DuckDB.
    """
    opts = {
        **theme.GRID_OPTS,
        "rowSelection": "single",          # AG Grid 32 string API
        "suppressRowClickSelection": False,
        "domLayout": "normal",
        "suppressMenuHide": False,
    }
    if infinite:
        opts.update({
            "rowBuffer": 0,
            "cacheBlockSize": BLOCK,
            "cacheOverflowSize": 1,
            "maxConcurrentDatasourceRequests": 2,
            "infiniteInitialRowCount": BLOCK,
            "maxBlocksInCache": 12,
        })
    opts.update(extra_options or {})

    kw = dict(
        id=gid,
        className=f"{theme.GRID_THEME[mode]} st-grid",
        columnDefs=columns or [],
        defaultColDef=theme.DEFAULT_COL,
        dashGridOptions=opts,
        style={"height": height, "width": "100%"},
    )
    if infinite:
        kw["rowModelType"] = "infinite"
    else:
        kw["rowData"] = rows or []
    if row_id:
        kw["getRowId"] = f"params.data.{row_id}"
    return dag.AgGrid(**kw)


def pill(text: str, mode: str = "dark", hue: str | None = None):
    color = theme.SLOTS[mode].get(hue or "", None)
    kw = {"style": {"color": color}} if color else {}
    return dmc.Badge(text, variant="light", radius="sm", size="sm", **kw)
