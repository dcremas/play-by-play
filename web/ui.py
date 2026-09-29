"""Shared UI primitives: stat tiles, section headers, grid factory, caveat notes."""
from __future__ import annotations

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import dcc, html

from . import theme

BLOCK = 120  # infinite-scroll block size


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


def hint(text, tone: str = "neutral"):
    """The same caveat as `note`, folded into a dot beside the control it is about.

    A note is a line of prose that sits under its control forever. In the body that is
    right -- a reader meets a number and the caveat is next to it. In the SIDEBAR it was
    not: two of these blocks were 39% of the panel's height, permanently, long after they
    had been read once, and they pushed the filters they explained below the fold.

    So the text is unchanged and still attached to its own control; it is one hover away
    rather than always open. `events` includes focus and touch, because a hover-only
    affordance is unreachable by keyboard and on a tablet.

    Returns None for empty text -- several of these are per-lens and one lens has nothing
    to say -- and Dash renders None as nothing, so the dot is absent rather than present
    and empty.
    """
    if not text:
        return None
    return dmc.Tooltip(
        label=text, multiline=True, w=300, withArrow=True, position="right",
        openDelay=100, closeDelay=80, zIndex=1500,
        events={"hover": True, "focus": True, "touch": True},
        children=dmc.Text("i", span=True, className=f"hint-dot hint-{tone}", tabIndex=0),
    )


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
