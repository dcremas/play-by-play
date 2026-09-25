"""CFB Play-by-Play Instance Explorer.

A Dash application over the DuckDB snapshot, aimed at the individual instance: one
row per play, filterable down to a single player in a single situation, with every
row opening into who was actually on the field for it.

The first choice is the side. Offense, defense and special teams are three lenses on
one corpus -- see web/lens.py -- and picking one sets the vocabulary for everything
below it: which phases exist, what a team column means, what a row is called, which
measures are on the tiles. The sidebar is one static set of controls that relabels
and reveals itself per lens rather than three sidebars, so every filter keeps its
value when the side changes and nothing has to be re-picked.

Run:  .venv/bin/python -m web.app
"""
from __future__ import annotations

import dash
import dash_mantine_components as dmc
from dash import Input, Output, State, ctx, dcc, html, no_update

from . import data, detail, league, lens, theme, ui
from .pages import explorer, player, team

app = dash.Dash(
    __name__,
    title="CFB Play-by-Play Explorer",
    external_stylesheets=dmc.styles.ALL,
    suppress_callback_exceptions=True,
    update_title=None,
)
server = app.server

S0, S1 = data.season_bounds()
D0, D1 = data.dist_bounds()
META = data.snapshot_meta()



# --------------------------------------------------------------------------- sidebar
def _label(text: str, cid: str | None = None):
    kw = {"id": cid} if cid else {}
    return dmc.Text(text, className="filter-label", mt=6, **kw)


def sidebar() -> list:
    key = lens.DEFAULT
    return [
        _label("Phase"),
        dmc.ChipGroup(
            id="f-phases", multiple=True, value=lens.default_chips(key),
            children=dmc.Group(gap=4, id="f-phase-chips", children=[
                dmc.Chip(lens.PHASES[key][k], value=k, size="xs", variant="light")
                for k in lens.chips(key)
            ]),
        ),
        html.Div(id="f-phase-note"),
        _label(f"Seasons  {S0}–{S1}"),
        dmc.RangeSlider(
            id="f-seasons", min=S0, max=S1, step=1, value=[S0, S1], minRange=0,
            size="sm", mb="lg", mt=4,
            marks=[{"value": s, "label": str(s)[2:]} for s in range(S0, S1 + 1)],
        ),
        _label(lens.SUBJECT[key], "lbl-teams"),
        # No `limit`: 249 teams render fine, and a limit made the list look finished
        # at the 60th one alphabetically with the rest reachable only by typing.
        dmc.MultiSelect(id="f-teams", data=data.team_options(key), value=[], size="xs",
                        searchable=True, clearable=True, hidePickedOptions=True,
                        placeholder="All teams", nothingFoundMessage="No team"),
        _label(lens.PLAYER[key], "lbl-players"),
        dmc.MultiSelect(id="f-players", data=[], value=[], size="xs", searchable=True,
                        clearable=True, limit=200, hidePickedOptions=True,
                        placeholder="All players — type to search",
                        nothingFoundMessage="Nobody over the volume floor"),
        html.Div(id="f-player-note"),
        _label("Outcome"),
        dmc.MultiSelect(id="f-outcomes", data=data.outcome_options(key), value=[],
                        size="xs", clearable=True, placeholder="All outcomes"),
        dmc.Space(h=6),
        dmc.Accordion(
            id="f-more", value=None, variant="separated", radius="sm", chevronPosition="right",
            children=[
                dmc.AccordionItem(value="opp", children=[
                    dmc.AccordionControl(dmc.Text("Opponent & competition", size="xs", fw=600)),
                    dmc.AccordionPanel([
                        _label(lens.OPPONENT[key], "lbl-opponents"),
                        dmc.MultiSelect(id="f-opponents", data=data.team_options(key),
                                        value=[], size="xs", searchable=True,
                                        clearable=True,
                                        placeholder="All opponents"),
                        _label("Conference", "lbl-conferences"),
                        dmc.MultiSelect(id="f-conferences",
                                        data=data.conference_options(key),
                                        value=[], size="xs", searchable=True, clearable=True,
                                        placeholder="All conferences"),
                        _label("Season type"),
                        dmc.MultiSelect(id="f-season-types",
                                        data=["regular", "postseason"], value=[],
                                        size="xs", clearable=True, placeholder="Both"),
                        dmc.Space(h=8),
                        # College-only. The NFL has no second division, so the control
                        # would be a checkbox that never changes the answer -- worse than
                        # absent, because it implies it might.
                        html.Div(id="f-fbs-wrap", children=[
                            dmc.Switch(id="f-fbs", label="FBS vs FBS only", size="xs",
                                       checked=False),
                            dmc.Space(h=6),
                        ]),
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
                        _label("Score state", "lbl-score-state"),
                        dmc.ChipGroup(id="f-score-states", multiple=True, value=[],
                                      children=dmc.Group(gap=4, children=[
                                          dmc.Chip(s, value=s, size="xs", variant="light")
                                          for s in ("Leading", "Tied", "Trailing")
                                      ])),
                        # Down and field position are the first questions anyone asks
                        # of a scrimmage play and mean nothing on a kickoff; kick
                        # distance is the reverse. Both live here and one is hidden.
                        html.Div(id="f-scrim-wrap", children=[
                            _label("Down"),
                            dmc.ChipGroup(id="f-downs", multiple=True, value=[],
                                          children=dmc.Group(gap=4, children=[
                                              dmc.Chip(str(i), value=str(i), size="xs",
                                                       variant="light")
                                              for i in (1, 2, 3, 4)
                                          ])),
                            _label("Field zone"),
                            dmc.MultiSelect(id="f-zones",
                                            data=data.zone_options(lens.DEFAULT),
                                            value=[], size="xs", clearable=True,
                                            placeholder="Anywhere on the field"),
                        ]),
                        html.Div(id="f-kick-wrap", children=[
                            _label("Kick distance (yd)"),
                            dmc.RangeSlider(id="f-dist", min=D0, max=D1, step=1,
                                            value=[D0, D1], size="sm", mb="md", mt=4,
                                            marks=[{"value": v, "label": str(v)}
                                                   for v in (D0, 30, 60, 90, D1)]),
                        ]),
                        dmc.Switch(id="f-clutch", size="xs", checked=False,
                                   label="Clutch only (4th qtr / OT, within 8)"),
                    ]),
                ]),
                dmc.AccordionItem(value="env", children=[
                    dmc.AccordionControl(dmc.Text("Environment & data quality", size="xs",
                                                  fw=600)),
                    dmc.AccordionPanel([
                        _label("Surface"),
                        dmc.MultiSelect(id="f-surfaces",
                                        data=data.surface_options(key),
                                        value=[], size="xs", clearable=True,
                                        placeholder="Both"),
                        dmc.Space(h=8),
                        # A Select, not a chip pair: the two sides are not complements.
                        # `indoor` is a STADIUM property -- 5 of the 18 indoor venues are
                        # retractable and read true whether or not the roof was open that
                        # day -- and it is NULL where the feed gives no venue. So "Indoor
                        # only" is not "everything the other chip excludes", and offering
                        # them as toggles would imply it was.
                        dmc.Select(id="f-roof", size="xs", value="any",
                                   allowDeselect=False, label="Roof",
                                   data=[{"value": "any", "label": "Either"},
                                         {"value": "indoor", "label": "Indoor only"},
                                         {"value": "outdoor", "label": "Outdoor only"}]),
                        ui.note("Indoor is the stadium, not the day: 5 of the 18 indoor "
                                "venues have a retractable roof and read indoor whether "
                                "or not it was open.", "info"),
                        _label("Division", "lbl-divisions"),
                        # Chips are built per league: FBS/FCS for college, the eight NFL
                        # divisions for the NFL. Two different columns upstream, one facet
                        # here -- see web/league.py.
                        dmc.ChipGroup(id="f-divisions", multiple=True, value=[],
                                      children=html.Div(id="f-division-chips")),
                        dmc.Space(h=8),
                        dmc.Select(id="f-neutral", size="xs", value="any",
                                   allowDeselect=False, label="Neutral site",
                                   data=[{"value": "any", "label": "Either"},
                                         {"value": "only", "label": "Neutral only"},
                                         {"value": "exclude", "label": "Exclude neutral"}]),
                        dmc.Space(h=8),
                        dmc.Switch(id="f-linked", size="xs", checked=False,
                                   label="Only rows linked to an athlete id"),
                        dmc.Space(h=6),
                        html.Div(id="f-linked-note"),
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
            dmc.Anchor(dmc.Text("Play-by-Play", className="st-brand", size="sm"),
                       href="/", underline="never", c="inherit"),
            # WHICH corpus, then which perspective on it. Two selectors rather than one
            # combined list of six, because the two choices are independent: every lens
            # works in both leagues. League comes first because it is the outer scope --
            # changing it changes what every number on the page counts.
            dmc.SegmentedControl(id="league-pick", value=league.DEFAULT,
                                 data=league.segmented(), size="xs",
                                 persistence=True, persistence_type="local"),
            dmc.SegmentedControl(id="lens-pick", value=lens.DEFAULT,
                                 data=lens.segmented(), size="xs",
                                 persistence=True, persistence_type="local"),
            dmc.Divider(orientation="vertical"),
            dmc.Text(id="corpus-count", size="xs", c="dimmed"),
            # A season still being played is in the corpus like any other, and its rows
            # look like any other. This is the only thing that says it is three weeks deep.
            # Per league: both corpora happen to have a live 2026, but they are different
            # numbers of games and only one of them is on screen.
            html.Div(id="partial-badges", style={"display": "flex", "gap": "8px"}),
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
        dcc.Store(id="lens", data=lens.DEFAULT, storage_type="local"),
        dcc.Store(id="league", data=league.DEFAULT, storage_type="local"),
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


# --------------------------------------------------------------------------- league
@app.callback(Output("league", "data"), Input("league-pick", "value"))
def _set_league(value):
    return league.resolve(value)


@app.callback(Output("partial-badges", "children"), Input("league", "data"))
def _partial_badges(lg):
    return [
        dmc.Tooltip(
            label=(f"{r['season']} is still being played — {r['games']:,} games, "
                   f"{r['plays']:,} plays, through week {r['week']}. Counts and rates "
                   f"for it are partial. Refresh with scripts/update_season.py "
                   f"{r['season']} --league {league.resolve(lg)}."),
            multiline=True, w=320,
            children=dmc.Badge(f"{r['season']} partial", variant="light", size="sm",
                               radius="sm", color="yellow"))
        for r in data.in_progress_seasons(league.resolve(lg))
    ]


@app.callback(Output("corpus-count", "children"), Input("league", "data"))
def _corpus_line(lg):
    """The header's corpus line. Per league, because the two are different sizes and a
    single hardcoded total would be wrong for whichever one is not on screen."""
    k = league.resolve(lg)
    n = league.corpus(k, "plays") + league.corpus(k, "kicks")
    return f"{n:,} plays · {league.corpus(k, 'games'):,} games · {league.corpus(k, 'seasons')}"


# --------------------------------------------------------------------------- lens
@app.callback(Output("lens", "data"), Input("lens-pick", "value"))
def _set_lens(value):
    return lens.resolve(value)


@app.callback(
    Output("f-phase-chips", "children"),
    Output("f-phases", "value"),
    Output("f-phase-note", "children"),
    Input("lens", "data"),
    Input("f-reset", "n_clicks"),
)
def _phase_chips(key, _n_reset):
    """The phase vocabulary is per lens, so the chips are rebuilt rather than reused.

    Anything left selected from the previous lens is dropped here; lens.kinds_for
    also refuses to honour a foreign chip, so the transient state between this
    callback and the filter store can never produce a wrong WHERE clause.
    """
    key = lens.resolve(key)
    chips = [dmc.Chip(lens.PHASES[key][k], value=k, size="xs", variant="light")
             for k in lens.chips(key)]
    note = None
    if key == "st":
        note = ui.note(
            "Conversions — extra points, two-point tries and the 75 defensive "
            "conversions — are off by default and nothing else about them is: they "
            "carry their own measures, leaderboard columns and profile tab. Off is "
            "only about distance — 67,678 of them are extra points from one spot, "
            "and leaving them on puts a spike at one distance in every distance "
            "view. Switch the chip on and the charts, columns and rates follow.",
            "neutral")
    return chips, lens.default_chips(key), note


@app.callback(
    Output("lbl-teams", "children"), Output("lbl-players", "children"),
    Output("lbl-opponents", "children"), Output("lbl-conferences", "children"),
    Output("lbl-score-state", "children"),
    Input("lens", "data"),
)
def _relabel(key):
    key = lens.resolve(key)
    return (lens.SUBJECT[key], lens.PLAYER[key], lens.OPPONENT[key],
            f"{lens.SUBJECT[key]} conference",
            f"Score state ({lens.SUBJECT[key].lower()})")


@app.callback(
    Output("f-teams", "data"), Output("f-opponents", "data"),
    Output("f-conferences", "data"), Output("f-surfaces", "data"),
    Output("f-outcomes", "data"), Output("f-zones", "data"),
    Output("f-division-chips", "children"),
    Output("f-divisions", "value"),
    Output("lbl-divisions", "children"),
    Output("f-fbs-wrap", "style"),
    Input("lens", "data"),
    Input("league", "data"),
    Input("f-reset", "n_clicks"),
)
def _option_lists(key, lg, _n_reset):
    """Every picker is scoped to BOTH axes. A team list that ignored the league would
    offer 281 teams from two leagues, and the ids collide -- team 2 is Auburn and the
    Buffalo Bills -- so the picked value would be ambiguous, not merely long."""
    key, lg = lens.resolve(key), league.resolve(lg)
    teams = data.team_options(key, lg)
    divs = data.division_options(key, lg)
    chips = dmc.Group(gap=4, children=[
        dmc.Chip(d, value=d, size="xs", variant="light") for d in divs])
    return (teams, teams, data.conference_options(key, lg), data.surface_options(key, lg),
            data.outcome_options(key, lg), data.zone_options(key, lg),
            # Cleared with the league: 'FBS' is not a value the NFL column can hold, so a
            # selection carried across leagues would match nothing and look like an empty
            # corpus rather than a stale filter.
            chips, [], league.division_label(lg),
            {} if league.has_fbs_filter(lg) else {"display": "none"})


@app.callback(
    Output("f-players", "value"),
    Output("f-outcomes", "value"),
    Input("lens", "data"),
    Input("league", "data"),
    Input("f-reset", "n_clicks"),
    State("f-outcomes", "value"),
)
def _lens_scoped_values(key, lg, _n_reset, outcomes):
    """The two facets whose *values* only mean something within one lens.

    An athlete id picked on one side rarely means anything on another -- a
    quarterback is not a tackler -- so the player picker empties with the side. It
    empties with the LEAGUE too, and for a harder reason: athlete ids are shared
    across the two corpora, so a college id left selected after a switch to the NFL
    silently matches that same man's pro plays rather than nothing.
    Outcomes survive where the new vocabulary still contains them: `Sack` and
    `Penalty` are in all three, `Touchback` is in none of the scrimmage ones, and a
    value the new lens cannot produce is dropped rather than left there silently
    matching no rows.

    Reset routes through here too. It is the same intent -- clear what is scoped --
    and one owner per output is what keeps Dash from rejecting the layout.
    """
    if ctx.triggered_id == "f-reset":
        return [], []
    valid = set(data.outcome_options(lens.resolve(key), league.resolve(lg)))
    return [], [o for o in (outcomes or []) if o in valid]


@app.callback(
    Output("f-scrim-wrap", "style"), Output("f-kick-wrap", "style"),
    Input("lens", "data"),
)
def _toggle_facets(key):
    scrim = lens.is_scrimmage(lens.resolve(key))
    return ({} if scrim else {"display": "none"},
            {"display": "none"} if scrim else {})


@app.callback(
    Output("f-player-note", "children"), Output("f-linked-note", "children"),
    Input("lens", "data"),
    Input("league", "data"),
)
def _player_notes(key, lg):
    key = lens.resolve(key)
    linked = {
        "off": "Switching this on keeps only plays where ESPN named a passer or a "
               "rusher — 99.9% of runs and passes, and none of the penalties.",
        "def": "This keeps only plays with a first tackler in the structured field, "
               "which drops 59% of rushes and 72% of passes. It is a coverage filter, "
               "not a quality one.",
        "st": "Athlete linking is thinnest in 2025 — switching this on drops unlinked "
              "kicks from every count.",
    }[key]
    return ui.note(lens.player_hint(key, lg), "neutral"), ui.note(linked, "info")


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
    ("downs", "f-downs", "value"),
    ("zones", "f-zones", "value"),
    ("dist", "f-dist", "value"),
    ("clutch_only", "f-clutch", "checked"),
    ("surfaces", "f-surfaces", "value"),
    ("divisions", "f-divisions", "value"),
    ("neutral", "f-neutral", "value"),
    ("roof", "f-roof", "value"),
    ("linked_only", "f-linked", "checked"),
]

# Reset values, by filter name. Built as a table rather than a positional list so
# adding a facet cannot silently shift what Reset puts in the one beside it.
_RESET = {
    "seasons": [S0, S1], "teams": [], "players": [], "outcomes": [], "opponents": [],
    "conferences": [], "season_types": [], "fbs_only": False, "conf_game": "any",
    "qtrs": [], "score_states": [], "downs": [], "zones": [], "dist": [D0, D1],
    "clutch_only": False, "surfaces": [], "divisions": [], "neutral": "any",
    "roof": "any", "linked_only": False,
}

# phases, players, outcomes and divisions are reset by the lens/league-scoped
# callbacks above, which already own those outputs -- divisions joined them when the
# league axis arrived, because 'FBS' is not a value the NFL column can hold. Dash
# allows one writer per output, so they are not in the list the Reset button writes;
# each of those callbacks takes f-reset as an Input instead.
_RESET_TARGETS = [t for t in _FILTER_INPUTS
                  if t[0] not in ("phases", "players", "outcomes", "divisions")]


@app.callback(
    Output("flt", "data"),
    [Input(cid, prop) for _, cid, prop in _FILTER_INPUTS],
    Input("lens", "data"),
    Input("league", "data"),
)
def _collect(*vals):
    *facets, key, lg = vals
    facets = facets[:len(_FILTER_INPUTS)]
    f = {name: v for (name, _, _), v in zip(_FILTER_INPUTS, facets)}
    f["lens"] = lens.resolve(key)
    # Every query in web/data.py filters on this; see where_from_filters, where it is the
    # one predicate that is never optional.
    f["league"] = league.resolve(lg)
    # A phase chip group emptied to nothing means "all", not "none" -- an empty
    # explorer is never what the click meant.
    if not f.get("phases"):
        f["phases"] = lens.default_chips(f["lens"])
    return f


@app.callback(
    Output("f-players", "data"),
    Input("f-phases", "value"),
    Input("lens", "data"),
    Input("league", "data"),
)
def _player_options(phases, key, lg):
    """Scope the player picker to the lens and the selected phases -- a punter is not
    a candidate when only field goals are on screen, and neither is a quarterback."""
    key = lens.resolve(key)
    chips = [c for c in (phases or []) if c in lens.PHASES[key]]
    return data.player_options(key, tuple(sorted(chips or lens.default_chips(key))),
                               league.resolve(lg))


@app.callback(
    [Output(cid, prop) for _, cid, prop in _RESET_TARGETS],
    Input("f-reset", "n_clicks"),
    prevent_initial_call=True,
)
def _reset(_n):
    return [_RESET[name] for name, _, _ in _RESET_TARGETS]


# --------------------------------------------------------------------------- routing
def _split_profile(parts):
    """('team'|'player', league, id) from a profile path, or None.

    Two shapes, both supported on purpose:

        /team/nfl/2      league-qualified, and what every link in the app now emits
        /team/2          the pre-NFL shape, read as college

    The qualified form exists because a bare id is genuinely ambiguous once there are
    two leagues -- team 2 is Auburn AND the Buffalo Bills -- so a shared or bookmarked
    link has to say which. The bare form keeps resolving to college rather than 404ing,
    because every link that existed before 2026-09-11 meant college.
    """
    if len(parts) == 3 and parts[0] in ("team", "player"):
        return parts[0], league.resolve(parts[1]), parts[2]
    if len(parts) == 2 and parts[0] in ("team", "player"):
        return parts[0], league.DEFAULT, parts[1]
    return None


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

    hit = _split_profile(parts)
    if hit:
        kind, lg, raw = hit
        page = player if kind == "player" else team
        try:
            return page.layout(int(raw), lg, mode), page.crumb(int(raw), lg)
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
    State("lens", "data"),
    State("mode", "data"),
    prevent_initial_call=True,
)
def _open_detail(uid, key, mode):
    if not uid:
        return False, no_update, no_update
    body, title = detail.render(uid, lens.resolve(key), mode or "dark")
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
