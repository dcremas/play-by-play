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

from . import data, detail, league, lens, routes, theme, ui
from .pages import explorer, player, team

app = dash.Dash(
    __name__,
    title="CFB Play-by-Play Explorer",
    external_stylesheets=dmc.styles.ALL,
    suppress_callback_exceptions=True,
    update_title=None,
    # `/` locally, `/plays/` on the box, where the root of this host is the
    # Streamlit agent. Sets BOTH prefixes, because nginx proxies the path through
    # unchanged -- so the routes Flask registers and the URLs the browser asks
    # for are the same strings. See web/routes.py.
    url_base_pathname=routes.BASE,
)
server = app.server

S0, S1 = data.season_bounds()
D0, D1 = data.dist_bounds()
META = data.snapshot_meta()



# --------------------------------------------------------------------------- sidebar
def _label(text: str, cid: str | None = None, *, hint=None, hint_id: str | None = None):
    """One label style for every control in this panel.

    Mantine's own `label=` prop on Select and NumberInput renders sentence-case body
    text, which put "Roof" and "Conference games" in a different voice from the
    uppercase "OUTCOME" and "SURFACE" two controls above them. Nothing in the sidebar
    uses that prop any more; everything comes through here.

    `hint` is a static caveat dot, `hint_id` an empty slot for one a callback fills
    per lens or league. The Group is `nowrap` with the dot at the end, so a two-word
    label plus a dot never wraps to a second line and changes the panel's rhythm.
    """
    kw = {"id": cid} if cid else {}
    right = hint if hint is not None else (html.Div(id=hint_id) if hint_id else None)
    label = dmc.Text(text, className="filter-label", **kw)
    if right is None:
        return dmc.Box(label, mt=6)
    return dmc.Group(gap=5, align="center", wrap="nowrap", mt=6, children=[label, right])


def _group(title: str, value: str, badge_id: str, children):
    """One collapsible facet group inside `More filters`.

    The badge is not decoration. These panels are shut by default and hold filters that
    change every number on the page, so a closed panel has to say whether anything
    inside it is on -- otherwise the first symptom of a stale Roof or Division filter is
    a count that looks wrong for no visible reason.
    """
    return dmc.AccordionItem(value=value, children=[
        dmc.AccordionControl(dmc.Group(gap=6, align="center", children=[
            dmc.Text(title, size="xs", fw=600),
            html.Div(id=badge_id),
        ])),
        dmc.AccordionPanel(children),
    ])


def sidebar() -> list:
    key = lens.DEFAULT
    return [
        # A title for the panel, and the one action that applies to all of it. Reset
        # lived at the very bottom, under the disclosure -- which put it off-screen
        # exactly when a stale filter was the thing you wanted undone. It is still
        # conditional: absent until something is actually set.
        dmc.Group(justify="space-between", align="center", mb=4, children=[
            dmc.Text("Filters", className="st-panel-title"),
            html.Div(id="f-reset-wrap", style={"display": "none"},
                     children=dmc.Button("Reset", id="f-reset", variant="subtle",
                                         size="compact-xs", c="dimmed")),
        ]),
        dmc.Divider(mb=4),
        _label("Phase", hint_id="f-phase-note"),
        dmc.ChipGroup(
            id="f-phases", multiple=True, value=lens.default_chips(key),
            children=dmc.Group(gap=4, id="f-phase-chips", children=[
                dmc.Chip(lens.PHASES[key][k], value=k, size="sm", variant="light")
                for k in lens.chips(key)
            ]),
        ),
        # Seasons used to sit here, between Phase and Team. It is in the main pane now
        # -- see season_band() for why a 264px track was the wrong home for it.
        dmc.Divider(className="st-facet-rule"),
        _label(lens.SUBJECT[key], "lbl-teams"),
        # No `limit`: 249 teams render fine, and a limit made the list look finished
        # at the 60th one alphabetically with the rest reachable only by typing.
        dmc.MultiSelect(id="f-teams", data=data.team_options(key), value=[], size="sm",
                        searchable=True, clearable=True, hidePickedOptions=True,
                        placeholder="All teams", nothingFoundMessage="No team"),
        _label(lens.PLAYER[key], "lbl-players", hint_id="f-player-note"),
        dmc.MultiSelect(id="f-players", data=[], value=[], size="sm", searchable=True,
                        clearable=True, limit=200, hidePickedOptions=True,
                        placeholder="All players \u2014 type to search",
                        nothingFoundMessage="Nobody over the volume floor"),
        dmc.Divider(className="st-facet-rule"),
        _label("Outcome"),
        dmc.MultiSelect(id="f-outcomes", data=data.outcome_options(key), value=[],
                        size="sm", clearable=True, placeholder="All outcomes"),
        dmc.Space(h="sm"),
        # One disclosure, not three. The five facets above answer most questions on
        # their own; everything below is a narrowing you reach for occasionally, and as
        # three separate bordered cards it cost 163px of the panel to say so. Shut, this
        # is one row -- with a count, so "occasionally" never becomes "invisibly on".
        # A plain show/hide, NOT a second Accordion around the first. Mantine animates a
        # panel to a height it measures at open and holds it with `overflow: hidden`, so
        # nesting one inside another makes the outer height a stale measurement of the
        # inner one the moment a group expands -- content that grows inside gets clipped
        # rather than pushing the panel down. A toggled `display` measures nothing, so
        # the groups grow the panel the way they should, and the disclosure cannot
        # animate out of step with what it contains. Open state is the click count's
        # parity: one Input, no store.
        dmc.Paper(withBorder=True, radius="sm", className="f-drawer", children=[
            dmc.UnstyledButton(id="f-drawer-toggle", className="f-drawer-toggle", w="100%",
                               children=dmc.Group(justify="space-between", align="center",
                                                  wrap="nowrap", children=[
                    dmc.Group(gap=6, align="center", children=[
                        dmc.Text("More filters", size="xs", fw=600),
                        html.Div(id="f-count-all"),
                    ]),
                    dmc.Text("\u2304", id="f-drawer-chevron", className="f-chevron"),
                ])),
            html.Div(id="f-drawer-body", style={"display": "none"}, children=dmc.Accordion(
                    id="f-more", value=[], multiple=True, variant="default",
                    chevronPosition="right", className="f-subgroups",
                    children=[
                        _group("Opponent & competition", "opp", "f-count-opp", [
                            _label(lens.OPPONENT[key], "lbl-opponents"),
                            dmc.MultiSelect(id="f-opponents", data=data.team_options(key),
                                            value=[], size="xs", searchable=True,
                                            clearable=True,
                                            placeholder="All opponents"),
                            _label("Conference", "lbl-conferences"),
                            dmc.MultiSelect(id="f-conferences",
                                            data=data.conference_options(key),
                                            value=[], size="xs", searchable=True,
                                            clearable=True,
                                            placeholder="All conferences"),
                            _label("Season type"),
                            dmc.MultiSelect(id="f-season-types",
                                            data=["regular", "postseason"], value=[],
                                            size="xs", clearable=True,
                                            placeholder="Both"),
                            _label("Conference games"),
                            dmc.Select(id="f-conf-game", size="xs", value="any",
                                       allowDeselect=False,
                                       data=[{"value": "any", "label": "Either"},
                                             {"value": "only", "label": "Conference only"},
                                             {"value": "exclude",
                                              "label": "Non-conference only"}]),
                            dmc.Space(h=10),
                            # College-only. The NFL has no second division, so the control
                            # would be a checkbox that never changes the answer -- worse
                            # than absent, because it implies it might.
                            html.Div(id="f-fbs-wrap", children=[
                                dmc.Switch(id="f-fbs", label="FBS vs FBS only", size="xs",
                                           checked=False),
                            ]),
                        ]),
                        _group("Situation", "sit", "f-count-sit", [
                            _label("Quarter"),
                            dmc.ChipGroup(id="f-qtrs", multiple=True, value=[],
                                          children=dmc.Group(gap=4, children=[
                                              dmc.Chip(str(i), value=str(i), size="xs",
                                                       variant="light")
                                              for i in (1, 2, 3, 4, 5)
                                          ])),
                            _label("Score state", "lbl-score-state"),
                            dmc.ChipGroup(id="f-score-states", multiple=True, value=[],
                                          children=dmc.Group(gap=4, children=[
                                              dmc.Chip(s, value=s, size="xs",
                                                       variant="light")
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
                                                       for v in (D0, 25, 50, 75, D1)]),
                            ]),
                            dmc.Space(h=6),
                            dmc.Switch(id="f-clutch", size="xs", checked=False,
                                       label="Clutch only (4th qtr / OT, within 8)"),
                        ]),
                        _group("Environment & data quality", "env", "f-count-env", [
                            _label("Surface"),
                            dmc.MultiSelect(id="f-surfaces",
                                            data=data.surface_options(key),
                                            value=[], size="xs", clearable=True,
                                            placeholder="Both"),
                            # A Select, not a chip pair: the two sides are not
                            # complements. `indoor` is a STADIUM property -- 5 of the 18
                            # indoor venues are retractable and read true whether or not
                            # the roof was open that day -- and it is NULL where the feed
                            # gives no venue. So "Indoor only" is not "everything the
                            # other chip excludes", and offering them as toggles would
                            # imply it was. The caveat is on the dot beside the label.
                            _label("Roof", hint=ui.hint(
                                "Indoor is the stadium, not the day: 5 of the 18 indoor "
                                "venues have a retractable roof and read indoor whether "
                                "or not it was open. The column is NULL where the feed "
                                "names no venue.", "info")),
                            dmc.Select(id="f-roof", size="xs", value="any",
                                       allowDeselect=False,
                                       data=[{"value": "any", "label": "Either"},
                                             {"value": "indoor", "label": "Indoor only"},
                                             {"value": "outdoor", "label": "Outdoor only"}]),
                            _label("Division", "lbl-divisions"),
                            # Chips are built per league: FBS/FCS for college, the eight
                            # NFL divisions for the NFL. Two different columns upstream,
                            # one facet here -- see web/league.py.
                            dmc.ChipGroup(id="f-divisions", multiple=True, value=[],
                                          children=html.Div(id="f-division-chips")),
                            _label("Neutral site"),
                            dmc.Select(id="f-neutral", size="xs", value="any",
                                       allowDeselect=False,
                                       data=[{"value": "any", "label": "Either"},
                                             {"value": "only", "label": "Neutral only"},
                                             {"value": "exclude",
                                              "label": "Exclude neutral"}]),
                            dmc.Space(h=10),
                            dmc.Group(gap=5, align="center", wrap="nowrap", children=[
                                dmc.Switch(id="f-linked", size="xs", checked=False,
                                           label="Only rows linked to an athlete id"),
                                html.Div(id="f-linked-note"),
                            ]),
                        ]),
                    ],
            )),
        ]),
    ]


# --------------------------------------------------------------------------- shell
def _vrule():
    """A full-height rule between header blocks. Mantine's vertical Divider collapses
    to nothing inside a Group -- the flex row gives it no height to fill -- so the
    height is set here rather than left to the parent."""
    return dmc.Divider(orientation="vertical", className="st-vrule")


def _picker(caption: str, control):
    """A segmented control with a caption over it.

    The two controls are different questions -- which corpus, which side of the ball --
    and side by side with no captions they read as one six-option row that happens to
    have a gap in it. The caption is what says they are two axes.
    """
    return dmc.Stack(gap=3, children=[dmc.Text(caption, className="st-cap"), control])


def _readout(caption: str, cid: str):
    """A right-aligned live number with a caption. The selected count is the one figure
    on the header that moves with every click, so it is set in the readout face rather
    than in the dimmed body size the rest of the chrome uses."""
    return dmc.Stack(gap=2, align="flex-end", children=[
        dmc.Text(caption, className="st-cap"),
        dmc.Text(id=cid, className="st-readout"),
    ])


def season_band():
    """The season range, given the width it always needed.

    It lived in the sidebar between Phase and Team, on a 264px track. Thirteen year
    marks do not fit in 264px -- they ran together -- so the marks were every OTHER
    year, written as two digits, and the exact selection had to be read off a text
    label above the track because the marks could not carry it. Two thumbs in that
    space are also a small target for a filter this many questions start with.

    Up here it has the whole main pane: every season gets its own mark, the marks are
    four-digit years rather than `14` and `26`, and the thumbs have room to grab. The
    readout stays -- it is the one thing that says whether a single-season selection is
    2019 or 2019-2019 -- but it is now beside the track rather than a substitute for it.

    It is in AppShellMain rather than on the explorer page because it is a GLOBAL
    filter. The profile pages honour it too; a control that existed only on `/` would
    leave the store holding a season range with nothing on screen saying so.
    """
    return dmc.Paper(withBorder=True, radius="sm", className="st-season-band", mb="md",
                     children=dmc.Group(align="center", wrap="nowrap", gap="xl",
                                        children=[
        dmc.Stack(gap=1, className="st-season-head", children=[
            dmc.Text("Seasons", className="st-cap"),
            dmc.Text(id="lbl-seasons", className="st-season-readout"),
        ]),
        dmc.RangeSlider(
            id="f-seasons", min=S0, max=S1, step=1, value=[S0, S1], minRange=0,
            size="md", className="st-season-slider",
            marks=[{"value": s, "label": str(s)} for s in range(S0, S1 + 1)],
        ),
    ]))


def header():
    built = META["built_at"]
    stamp = built.strftime("%d %b %Y %H:%M") if built else "unknown"
    return dmc.Group(justify="space-between", w="100%", align="center", children=[
        dmc.Group(gap="md", align="center", wrap="nowrap", className="st-head-left",
                  children=[
            # The wordmark and what it is a mark OF. The corpus line used to float loose
            # in the middle of the header, where it read as a fourth control; under the
            # title it reads as the subtitle it always was.
            dmc.Anchor(href=routes.url("/"), underline="never", c="inherit",
                       children=dmc.Stack(gap=1, children=[
                           dmc.Text("Play-by-Play", className="st-brand"),
                           dmc.Text(id="corpus-count", className="st-brand-sub"),
                       ])),
            _vrule(),
            # WHICH corpus, then which perspective on it. Two selectors rather than one
            # combined list of six, because the two choices are independent: every lens
            # works in both leagues. League comes first because it is the outer scope --
            # changing it changes what every number on the page counts.
            _picker("League", dmc.SegmentedControl(
                id="league-pick", value=league.DEFAULT, data=league.segmented(),
                size="sm", radius="sm", persistence=True, persistence_type="local")),
            _picker("Side of the ball", dmc.SegmentedControl(
                id="lens-pick", value=lens.DEFAULT, data=lens.segmented(),
                size="sm", radius="sm", persistence=True, persistence_type="local")),
            # A season still being played is in the corpus like any other, and its rows
            # look like any other. This is the only thing that says it is three weeks deep.
            # Per league: both corpora happen to have a live 2026, but they are different
            # numbers of games and only one of them is on screen.
            html.Div(id="partial-badges", style={"display": "flex", "gap": "8px"}),
            html.Div(id="crumb"),
        ]),
        dmc.Group(gap="md", align="center", wrap="nowrap", className="st-head-right",
                  children=[
            _readout("Selected", "row-count"),
            _vrule(),
            dmc.Tooltip(label=f"Snapshot built {stamp}",
                        children=dmc.Badge("snapshot", variant="light", size="sm",
                                           radius="sm")),
            dmc.Switch(id="mode-toggle", size="md", checked=True, onLabel="D", offLabel="L",
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
            # 68 rather than 52: the header carries two lines now -- a caption over each
            # selector, and the corpus line under the wordmark -- and 52 clipped them.
            header={"height": 68},
            navbar={"width": 320, "breakpoint": "sm", "collapsed": {"mobile": True}},
            padding="md",
            children=[
                dmc.AppShellHeader(header(), className="st-header"),
                dmc.AppShellNavbar(dmc.Box(sidebar(), p="md"), className="st-navbar"),
                # The WIP notice sits ABOVE the loader, not inside it: it is a
                # property of the deployment rather than of the page being
                # rendered, so it must not blink out every time a filter change
                # swaps the body. `wip_banner()` returns None when the switch is
                # off, and Dash renders None as nothing.
                dmc.AppShellMain([
                    ui.wip_banner(),
                    # Above the loader, not inside it: the season range is a filter that
                    # OUTLIVES the page under it, and swapping it out on every route
                    # change would blink the control the reader is dragging.
                    season_band(),
                    dcc.Loading(
                        html.Div(id="page"), type="default", delay_show=250,
                        color=theme.SLOTS["dark"]["blue"],
                    ),
                ]),
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
    chips = [dmc.Chip(lens.PHASES[key][k], value=k, size="sm", variant="light")
             for k in lens.chips(key)]
    note = None
    if key == "st":
        note = ui.hint(
            "Conversions — extra points, two-point tries and the 75 defensive "
            "conversions — are off by default and nothing else about them is: they "
            "carry their own measures, leaderboard columns and profile tab. Off is "
            "only about distance — 67,678 of them are extra points from one spot, "
            "and leaving them on puts a spike at one distance in every distance "
            "view. Switch the chip on and the charts, columns and rates follow.")
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
    return ui.hint(lens.player_hint(key, lg)), ui.hint(linked, "info")


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


# --------------------------------------------------------------------------- active counts
# Which facets sit behind which disclosure. The order is the panel's order, and the
# three lists together are every facet EXCEPT the five that stayed above the fold --
# those are visible, so they need no badge to announce themselves.
_GROUPS = {
    "opp": ("opponents", "conferences", "season_types", "fbs_only", "conf_game"),
    "sit": ("qtrs", "score_states", "downs", "zones", "dist", "clutch_only"),
    "env": ("surfaces", "divisions", "neutral", "roof", "linked_only"),
}

# The value each facet holds when it is doing nothing. Read off _RESET where there is
# an entry, with the four the lens/league callbacks own added by hand -- those are
# cleared to empty by _lens_scoped_values and _option_lists rather than by _reset, so
# they are absent from that table.
_IDLE = {**_RESET, "players": [], "outcomes": [], "divisions": []}


def _is_active(name: str, value, key: str) -> bool:
    """Is this facet narrowing the selection right now?

    Lens-gated exactly the way data.where_from_filters is gated: down and field zone
    are dropped on the kicking lens and kick distance on the scrimmage ones, so a value
    left in a hidden control is not in the WHERE clause -- and must not be counted here
    either. A badge that claimed a filter the query does not apply would be worse than
    no badge, because the whole point of it is to explain a count.
    """
    if name in ("downs", "zones") and not lens.is_scrimmage(key):
        return False
    if name == "dist" and lens.is_scrimmage(key):
        return False
    idle = _IDLE.get(name)
    if isinstance(idle, list):
        return bool(value) and list(value) != idle
    return bool(value) and value != idle


def _count(flt, names) -> int:
    key = lens.resolve((flt or {}).get("lens"))
    return sum(1 for n in names if _is_active(n, (flt or {}).get(n), key))


def _badge(n: int):
    return dmc.Badge(str(n), size="xs", variant="filled", circle=True) if n else None


@app.callback(
    Output("f-count-all", "children"), Output("f-count-opp", "children"),
    Output("f-count-sit", "children"), Output("f-count-env", "children"),
    Output("f-reset-wrap", "style"),
    Input("flt", "data"),
)
def _active_counts(flt):
    """Counts on the shut disclosures, and the Reset link's presence.

    Reset appears on ANY active facet, including the five visible ones -- it resets
    those too -- while the badges cover only what is hidden. So the link can be on
    screen with every badge absent, which is correct: the panel is showing you the
    filter itself in that case.
    """
    per = {g: _count(flt, names) for g, names in _GROUPS.items()}
    key = lens.resolve((flt or {}).get("lens"))
    visible = _count(flt, ("seasons", "teams", "players", "outcomes"))
    phases = set((flt or {}).get("phases") or []) != set(lens.default_chips(key))
    any_on = sum(per.values()) + visible + int(phases)
    return (_badge(sum(per.values())), _badge(per["opp"]), _badge(per["sit"]),
            _badge(per["env"]), {} if any_on else {"display": "none"})


@app.callback(
    Output("f-drawer-body", "style"), Output("f-drawer-chevron", "className"),
    Input("f-drawer-toggle", "n_clicks"),
)
def _toggle_drawer(n):
    """Open on odd clicks. The chevron is a class rather than a second character, so
    the two states are one glyph rotating and cannot drift out of step with the panel."""
    if n and n % 2:
        return {}, "f-chevron f-chevron-open"
    return {"display": "none"}, "f-chevron"


@app.callback(Output("lbl-seasons", "children"), Input("f-seasons", "value"))
def _season_label(rng):
    """The exact selection, beside the track.

    The word "Seasons" is the caption above this now, so it is not repeated here. One
    season selected reads as one number, not as a range of it to itself -- which is the
    distinction the marks alone cannot draw, both thumbs sitting on the same mark.
    """
    lo, hi = (rng or [S0, S1])
    return f"{lo}" if lo == hi else f"{lo}\u2013{hi}"


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
    # `pathname` is the browser's address and carries the mount point; everything
    # below this line works in in-app routes. See web/routes.py.
    path = routes.strip(path).rstrip("/") or "/"
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
        dmc.Anchor("Back to the explorer", href=routes.url("/"), size="sm"),
    ])


# --------------------------------------------------------------------------- detail drawer
@app.callback(
    Output("detail-drawer", "opened"),
    Output("detail-body", "children"),
    Output("detail-drawer", "title"),
    Input("detail-uid", "data"),
    State("lens", "data"),
    # The corpus the drawer must read. play_uid does not collide between the two
    # (checked: zero shared ids), so reading the wrong one finds NOTHING rather
    # than the wrong play -- an empty drawer, not a lie. Passed explicitly anyway,
    # because "it happens to be safe" is not a property to rely on.
    State("league", "data"),
    State("mode", "data"),
    prevent_initial_call=True,
)
def _open_detail(uid, key, lg, mode):
    if not uid:
        return False, no_update, no_update
    body, title = detail.render(uid, league.resolve(lg), lens.resolve(key),
                                mode or "dark")
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
