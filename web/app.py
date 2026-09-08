"""Special Teams Instance Explorer.

A Dash application over the DuckDB snapshot, aimed at the individual instance: one
row per kick, filterable down to a single player in a single situation, with every
row opening into who was actually on the field for it.

Run:  .venv/bin/python -m web.app
"""
from __future__ import annotations

import dash
import dash_mantine_components as dmc
from dash import Input, Output, State, dcc, html, no_update

from . import data, detail, theme, ui
from .pages import explorer, player, team

app = dash.Dash(
    __name__,
    title="Special Teams Explorer",
    external_stylesheets=dmc.styles.ALL,
    suppress_callback_exceptions=True,
    update_title=None,
)
server = app.server

S0, S1 = data.season_bounds()
D0, D1 = data.dist_bounds()
META = data.snapshot_meta()
LIVE = data.in_progress_seasons()


# --------------------------------------------------------------------------- sidebar
def _label(text: str):
    return dmc.Text(text, className="filter-label", mt=6)


def sidebar() -> list:
    return [
        _label("Phase"),
        dmc.ChipGroup(
            id="f-phases", multiple=True, value=list(data.PHASE_ORDER),
            children=dmc.Group(gap=4, children=[
                dmc.Chip(data.PHASES[k], value=k, size="xs", variant="light")
                for k in data.PHASE_ORDER
            ]),
        ),
        _label(f"Seasons  {S0}–{S1}"),
        dmc.RangeSlider(
            id="f-seasons", min=S0, max=S1, step=1, value=[S0, S1], minRange=0,
            size="sm", mb="lg", mt=4,
            marks=[{"value": s, "label": str(s)[2:]} for s in range(S0, S1 + 1)],
        ),
        _label("Kicking team"),
        dmc.MultiSelect(id="f-teams", data=data.team_options(), value=[], size="xs",
                        searchable=True, clearable=True, limit=60, hidePickedOptions=True,
                        placeholder="All teams", nothingFoundMessage="No team"),
        _label("Kicker / punter"),
        dmc.MultiSelect(id="f-players", data=[], value=[], size="xs", searchable=True,
                        clearable=True, limit=60, hidePickedOptions=True,
                        placeholder="All players",
                        nothingFoundMessage="No player with 3+ kicks"),
        _label("Outcome"),
        dmc.MultiSelect(id="f-outcomes", data=data.outcome_options(), value=[],
                        size="xs", clearable=True, placeholder="All outcomes"),
        dmc.Space(h=6),
        dmc.Accordion(
            id="f-more", value=None, variant="separated", radius="sm", chevronPosition="right",
            children=[
                dmc.AccordionItem(value="opp", children=[
                    dmc.AccordionControl(dmc.Text("Opponent & competition", size="xs", fw=600)),
                    dmc.AccordionPanel([
                        _label("Opponent"),
                        dmc.MultiSelect(id="f-opponents", data=data.team_options(), value=[],
                                        size="xs", searchable=True, clearable=True, limit=60,
                                        placeholder="All opponents"),
                        _label("Kicking team conference"),
                        dmc.MultiSelect(id="f-conferences", data=data.conference_options(),
                                        value=[], size="xs", searchable=True, clearable=True,
                                        placeholder="All conferences"),
                        _label("Season type"),
                        dmc.MultiSelect(id="f-season-types",
                                        data=["regular", "postseason"], value=[],
                                        size="xs", clearable=True, placeholder="Both"),
                        dmc.Space(h=8),
                        dmc.Switch(id="f-fbs", label="FBS vs FBS only", size="xs",
                                   checked=False),
                        dmc.Space(h=6),
                        dmc.Select(id="f-conf-game", size="xs", value="any",
                                   allowDeselect=False, label="Conference games",
                                   data=[{"value": "any", "label": "Either"},
                                         {"value": "only", "label": "Conference only"},
                                         {"value": "exclude", "label": "Non-conference only"}]),
                    ]),
                ]),
                dmc.AccordionItem(value="sit", children=[
                    dmc.AccordionControl(dmc.Text("Situation", size="xs", fw=600)),
                    dmc.AccordionPanel([
                        _label("Quarter"),
                        dmc.ChipGroup(id="f-qtrs", multiple=True, value=[],
                                      children=dmc.Group(gap=4, children=[
                                          dmc.Chip(str(i), value=str(i), size="xs",
                                                   variant="light") for i in (1, 2, 3, 4, 5)
                                      ])),
                        _label("Score state (kicking team)"),
                        dmc.ChipGroup(id="f-score-states", multiple=True, value=[],
                                      children=dmc.Group(gap=4, children=[
                                          dmc.Chip(s, value=s, size="xs", variant="light")
                                          for s in ("Leading", "Tied", "Trailing")
                                      ])),
                        _label("Kick distance (yd)"),
                        dmc.RangeSlider(id="f-dist", min=D0, max=D1, step=1,
                                        value=[D0, D1], size="sm", mb="md", mt=4,
                                        marks=[{"value": v, "label": str(v)}
                                               for v in (D0, 30, 60, 90, D1)]),
                        dmc.Switch(id="f-clutch", size="xs", checked=False,
                                   label="Clutch only (4th qtr / OT, within 8)"),
                    ]),
                ]),
                dmc.AccordionItem(value="env", children=[
                    dmc.AccordionControl(dmc.Text("Environment & data quality", size="xs",
                                                  fw=600)),
                    dmc.AccordionPanel([
                        _label("Surface"),
                        dmc.MultiSelect(id="f-surfaces", data=data.surface_options(),
                                        value=[], size="xs", clearable=True,
                                        placeholder="Both"),
                        _label("Division"),
                        dmc.ChipGroup(id="f-divisions", multiple=True, value=[],
                                      children=dmc.Group(gap=4, children=[
                                          dmc.Chip(d, value=d, size="xs", variant="light")
                                          for d in ("FBS", "FCS")
                                      ])),
                        dmc.Space(h=8),
                        dmc.Select(id="f-neutral", size="xs", value="any",
                                   allowDeselect=False, label="Neutral site",
                                   data=[{"value": "any", "label": "Either"},
                                         {"value": "only", "label": "Neutral only"},
                                         {"value": "exclude", "label": "Exclude neutral"}]),
                        dmc.Space(h=8),
                        dmc.Switch(id="f-linked", size="xs", checked=False,
                                   label="Only kicks linked to an athlete id"),
                        dmc.Space(h=6),
                        ui.note("Athlete linking is thinnest in 2025 — switching this "
                                "on drops unlinked kicks from every count.", "info"),
                    ]),
                ]),
            ],
        ),
        dmc.Space(h="md"),
        dmc.Button("Reset all filters", id="f-reset", variant="subtle", size="xs",
                   fullWidth=True),
    ]


# --------------------------------------------------------------------------- shell
def header():
    built = META["built_at"]
    stamp = built.strftime("%d %b %Y %H:%M") if built else "unknown"
    return dmc.Group(justify="space-between", w="100%", children=[
        dmc.Group(gap="sm", children=[
            dmc.Anchor(dmc.Text("Special Teams · Instance Explorer",
                                className="st-brand", size="sm"),
                       href="/", underline="never", c="inherit"),
            dmc.Divider(orientation="vertical"),
            dmc.Text(f"{META['rows']:,} kicks · {S0}–{S1}", size="xs", c="dimmed"),
            # A season still being played is in the corpus like any other, and its rows
            # look like any other. This is the only thing that says it is three weeks deep.
            *[dmc.Tooltip(
                label=(f"{r['season']} is still being played — {r['games']:,} games, "
                       f"{r['plays']:,} kicks, through week {r['week']}. Counts and rates "
                       f"for it are partial. Refresh with scripts/update_season.py "
                       f"{r['season']}."),
                multiline=True, w=320,
                children=dmc.Badge(f"{r['season']} partial", variant="light", size="sm",
                                   radius="sm", color="yellow"))
              for r in LIVE],
            html.Div(id="crumb"),
        ]),
        dmc.Group(gap="sm", children=[
            dmc.Text(id="row-count", size="xs", c="dimmed"),
            dmc.Tooltip(label=f"Snapshot built {stamp}",
                        children=dmc.Badge("snapshot", variant="light", size="sm",
                                           radius="sm")),
            dmc.Switch(id="mode-toggle", size="sm", checked=True, onLabel="D", offLabel="L",
                       persistence=True, persistence_type="local"),
        ]),
    ])


app.layout = dmc.MantineProvider(
    id="provider",
    forceColorScheme="dark",
    theme={"fontFamily": theme.FONT, "defaultRadius": "sm"},
    children=[
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="flt"),
        dcc.Store(id="mode", data="dark", storage_type="local"),
        dcc.Store(id="detail-uid"),
        dmc.AppShell(
            header={"height": 52},
            navbar={"width": 296, "breakpoint": "sm", "collapsed": {"mobile": True}},
            padding="md",
            children=[
                dmc.AppShellHeader(header(), className="st-header"),
                dmc.AppShellNavbar(dmc.Box(sidebar(), p="md"), className="st-navbar"),
                dmc.AppShellMain(dcc.Loading(
                    html.Div(id="page"), type="default", delay_show=250,
                    color=theme.SLOTS["dark"]["blue"],
                )),
            ],
        ),
        dmc.Drawer(id="detail-drawer", opened=False, position="right", size="46%",
                   padding="md", zIndex=1400, title="",
                   children=html.Div(id="detail-body")),
    ],
)


# --------------------------------------------------------------------------- mode
@app.callback(Output("mode", "data"), Input("mode-toggle", "checked"))
def _set_mode(dark):
    return "dark" if dark else "light"


@app.callback(Output("provider", "forceColorScheme"), Input("mode", "data"))
def _apply_mode(mode):
    return mode or "dark"


# --------------------------------------------------------------------------- filters
_FILTER_INPUTS = [
    ("phases", "f-phases", "value"),
    ("seasons", "f-seasons", "value"),
    ("teams", "f-teams", "value"),
    ("players", "f-players", "value"),
    ("outcomes", "f-outcomes", "value"),
    ("opponents", "f-opponents", "value"),
    ("conferences", "f-conferences", "value"),
    ("season_types", "f-season-types", "value"),
    ("fbs_only", "f-fbs", "checked"),
    ("conf_game", "f-conf-game", "value"),
    ("qtrs", "f-qtrs", "value"),
    ("score_states", "f-score-states", "value"),
    ("dist", "f-dist", "value"),
    ("clutch_only", "f-clutch", "checked"),
    ("surfaces", "f-surfaces", "value"),
    ("divisions", "f-divisions", "value"),
    ("neutral", "f-neutral", "value"),
    ("linked_only", "f-linked", "checked"),
]


@app.callback(
    Output("flt", "data"),
    [Input(cid, prop) for _, cid, prop in _FILTER_INPUTS],
)
def _collect(*vals):
    f = {key: v for (key, _, _), v in zip(_FILTER_INPUTS, vals)}
    # A phase chip group emptied to nothing means "all", not "none" -- an empty
    # explorer is never what the click meant.
    if not f.get("phases"):
        f["phases"] = list(data.PHASE_ORDER)
    return f


@app.callback(
    Output("f-players", "data"),
    Input("f-phases", "value"),
)
def _player_options(phases):
    """Scope the player picker to the selected phases -- a punter is not a candidate
    when only field goals are on screen."""
    return data.player_options(tuple(sorted(phases or data.PHASE_ORDER)))


@app.callback(
    [Output(cid, prop) for _, cid, prop in _FILTER_INPUTS],
    Input("f-reset", "n_clicks"),
    prevent_initial_call=True,
)
def _reset(_n):
    return [list(data.PHASE_ORDER), [S0, S1], [], [], [], [], [], [], False, "any",
            [], [], [D0, D1], False, [], [], "any", False]


# --------------------------------------------------------------------------- routing
@app.callback(
    Output("page", "children"),
    Output("crumb", "children"),
    Input("url", "pathname"),
    Input("mode", "data"),
)
def _route(path, mode):
    mode = mode or "dark"
    path = (path or "/").rstrip("/") or "/"
    parts = [p for p in path.split("/") if p]

    if len(parts) == 2 and parts[0] == "player":
        try:
            return player.layout(int(parts[1]), mode), player.crumb(int(parts[1]))
        except (ValueError, LookupError):
            return _not_found(path), None
    if len(parts) == 2 and parts[0] == "team":
        try:
            return team.layout(int(parts[1]), mode), team.crumb(int(parts[1]))
        except (ValueError, LookupError):
            return _not_found(path), None
    if parts:
        return _not_found(path), None
    return explorer.layout(mode), None


def _not_found(path):
    return dmc.Stack(align="center", mt=80, children=[
        dmc.Text("Nothing at this address", fw=600),
        dmc.Text(path, size="xs", c="dimmed"),
        dmc.Anchor("Back to the explorer", href="/", size="sm"),
    ])


# --------------------------------------------------------------------------- detail drawer
@app.callback(
    Output("detail-drawer", "opened"),
    Output("detail-body", "children"),
    Output("detail-drawer", "title"),
    Input("detail-uid", "data"),
    State("mode", "data"),
    prevent_initial_call=True,
)
def _open_detail(uid, mode):
    if not uid:
        return False, no_update, no_update
    body, title = detail.render(uid, mode or "dark")
    return True, body, title


@app.callback(
    Output("detail-drawer", "opened", allow_duplicate=True),
    Output("detail-uid", "data", allow_duplicate=True),
    Input("url", "pathname"),
    prevent_initial_call=True,
)
def _close_on_nav(_path):
    """Following a link out of the drawer should not leave it hanging over the new page."""
    return False, None


if __name__ == "__main__":
    app.run(debug=True, port=8060)
