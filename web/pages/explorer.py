"""The explorer: one filter set, one lens, two or three grains.

Plays is the point of the app -- up to 1.5M rows on AG Grid's infinite row model, so
sorting and per-column filtering are pushed down into DuckDB and the browser never
holds more than a dozen blocks. Teams is the same selection rolled up, and on the
kicks so is Kickers; clicking a row on either navigates to that entity's profile.

Which lens is active decides the vocabulary of everything on the page -- the tiles,
the charts, the columns, the grains, even the word for a row -- and the lens module
is the only place that knows the difference.
"""
from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, State, callback, dcc, html, no_update
from dash.exceptions import PreventUpdate

from .. import charts, columns, data, export, league, lens, routes, ui
from . import common


def layout(mode: str = "dark"):
    return dmc.Stack(gap="md", children=[
        html.Div(id="ex-kpis"),
        # Collapsed by default since 2026-09-09. The plays grid is what this page is
        # for, and starting with three 340px charts above it pushed the first row of
        # detail off most screens. The section keeps its label and chevron, so it reads
        # as collapsed rather than absent, and one click restores it. The chart
        # callbacks still fire while it is shut -- they are cheap against a local
        # snapshot, and gating them on the accordion would serve stale figures the
        # moment it opened.
        dmc.Accordion(
            value=None, variant="separated", radius="sm", chevronPosition="right",
            children=[dmc.AccordionItem(value="shape", children=[
                dmc.AccordionControl(dmc.Text("Shape of the current selection", size="xs",
                                              fw=600)),
                dmc.AccordionPanel(dmc.SimpleGrid(
                    id="ex-chartgrid", cols={"base": 1, "lg": 3}, spacing="sm",
                    children=[ui.graph("ex-c1", 320), ui.graph("ex-c2", 320),
                              ui.graph("ex-c3", 320)],
                )),
            ])],
        ),
        dmc.Group(justify="space-between", align="flex-end", children=[
            dmc.SegmentedControl(id="ex-grain", value="plays",
                                 data=lens.GRAINS[lens.DEFAULT], size="xs"),
            dmc.Group(gap="sm", align="flex-end", children=[
                html.Div(id="ex-minwrap", children=dmc.NumberInput(
                    id="ex-min", label="Min plays", value=10, min=1, max=5000, step=5,
                    size="xs", w=120)),
                dmc.MultiSelect(id="ex-cols", data=[], value=[], size="xs", w=290,
                                searchable=True, clearable=True,
                                label="Add columns", placeholder="Default set"),
            ]),
        ]),
        html.Div(id="ex-hint"),
        html.Div(id="ex-grid-wrap"),
        # One download sink for all three grains: a click writes exactly one file, so
        # three competing targets would buy nothing. The Store beside it is the plays
        # grid's last `getRowsRequest` -- its header filters and its sort order, kept
        # because the infinite row model means the browser cannot be asked for them.
        export.sink("ex-dl"),
        dcc.Store(id="ex-plays-req"),
    ])


# --------------------------------------------------------------------------- kpis
def _kick_kpis(flt, where, lg: str, chips):
    key = columns.key_for(chips)
    r = data.q(f"""
        SELECT count(*) n,
               count(DISTINCT player_id) players,
               count(DISTINCT team_id) teams,
               count(DISTINCT game_id) games,
               avg(kick_yds) dist,
               sum(CASE WHEN outcome = 'Unknown' THEN 1 ELSE 0 END) unk,
               sum(CASE WHEN outcome = 'Made' THEN 1 ELSE 0 END) made,
               sum(CASE WHEN play_kind = 'field_goal' AND outcome <> 'Negated'
                        THEN 1 ELSE 0 END) fg_att,
               sum(CASE WHEN outcome = 'Converted' THEN 1 ELSE 0 END) conv,
               sum(CASE WHEN play_kind IN ('pat', 'two_point', 'defensive_conversion')
                        THEN 1 ELSE 0 END) conv_att,
               avg(punt_net_yds) net,
               sum(CASE WHEN outcome = 'Touchback' THEN 1 ELSE 0 END) tb,
               sum(CASE WHEN play_kind = 'kickoff' AND NOT COALESCE(onside, FALSE)
                        THEN 1 ELSE 0 END) ko,
               count(player_id) linked
        FROM {lens.view("st", lg)} WHERE {where}
    """).iloc[0]

    n = int(r.n)
    if not n:
        return None, None
    tiles = [
        ui.tile("Kicks", f"{n:,}", f"in {int(r.games):,} games"),
        ui.tile("Kickers", f"{int(r.players):,}",
                f"{int(r.linked):,} of {n:,} kicks linked"),
        ui.tile("Teams", f"{int(r.teams):,}"),
        ui.tile("Mean distance", "—" if r.dist != r.dist else f"{r.dist:.1f} yd"),
    ]
    if key == "field_goal" and r.fg_att:
        tiles.append(ui.tile("Make rate", f"{r.made / r.fg_att:.1%}",
                             f"{int(r.made):,} of {int(r.fg_att):,}"))
    elif key == "punt" and r.net == r.net:
        tiles.append(ui.tile("Mean net", f"{r.net:.1f} yd"))
    elif key == "kickoff" and r.ko:
        tiles.append(ui.tile("Touchback rate", f"{r.tb / r.ko:.1%}",
                             "onside kicks excluded"))
    elif key == "conversion" and r.conv_att:
        tiles.append(ui.tile("Conversion rate", f"{r.conv / r.conv_att:.1%}",
                             f"{int(r.conv):,} of {int(r.conv_att):,}"))
    else:
        tiles.append(ui.tile("Phases", f"{len(chips)}",
                             ", ".join(lens.PHASES["st"][p] for p in chips)))

    # The "Unknown outcome" tile was dropped on 2026-09-09 at the user's request, and
    # the five-line caveat under it became the one line below. The number itself is not
    # hidden: `Unknown` is still a value in the Outcome column, still a filter, and
    # still a band in the mix chart. What it costs is denominator, never the numerator,
    # so no measure on these tiles is understated by it -- which is the one thing a
    # reader has to know and all that is left here.
    unk = int(r.unk)
    note = None
    if unk:
        note = ui.note(
            f"{unk:,} of these {n:,} kicks ({unk / n:.1%}) state no outcome in the play "
            "text and read as Unknown — they cost denominator, not numerator, so nothing "
            "above is understated by them.", "warn")
    return tiles, note


def _scrim_kpis(key, where, lg: str, chips):
    h = common.SCRIM_HEADERS[key]
    r = data.q(f"""
        SELECT count(*) n,
               count(DISTINCT team_id) teams,
               count(DISTINCT game_id) games,
               count(*) FILTER (play_kind IN ('rush', 'pass', 'sack')) snaps,
               avg(yards_gained) FILTER (play_kind IN ('rush', 'pass', 'sack')
                                         AND NOT COALESCE(is_turnover, FALSE)) ypp,
               count(*) FILTER (COALESCE(first_down_gained, FALSE)) fd,
               count(*) FILTER (COALESCE(is_turnover, FALSE)) turn,
               count(*) FILTER (COALESCE(is_touchdown, FALSE)) td,
               count(*) FILTER (yards_impossible) bad
        FROM {lens.view(key, lg)} WHERE {where}
    """).iloc[0]

    n = int(r.n)
    if not n:
        return None, None
    snaps = int(r.snaps)
    tiles = [
        ui.tile("Plays", f"{n:,}", f"in {int(r.games):,} games"),
        ui.tile("Teams", f"{int(r.teams):,}"),
        ui.tile(h["ypp"], "—" if r.ypp != r.ypp else f"{r.ypp:.2f} yd",
                f"over {snaps:,} snaps" if snaps else None),
        ui.tile(h["fd_rate"], f"{int(r.fd) / snaps:.1%}" if snaps else "—",
                f"{int(r.fd):,} of {snaps:,}"),
        ui.tile(h["turnovers"], f"{int(r.turn):,}",
                f"{int(r.turn) / snaps:.2%} of snaps" if snaps else None),
        ui.tile(h["tds"], f"{int(r.td):,}"),
    ]
    notes = [ui.note(
        "Per-play measures run on rush, pass and sack rows only — a penalty's "
        "statYardage is a penalty distance, not a gain — and mean yards additionally "
        "drops turnovers, because ESPN records the defense's return distance there "
        "rather than the offense's gain. Both denominators are on the tiles.",
        "neutral")]
    if int(r.bad):
        notes.append(ui.note(
            f"{int(r.bad)} of these rows carry an impossible statYardage (11,131 yards "
            "on one 2017 pass, −5,114 on a 2018 penalty). The value is NULLed rather "
            "than clamped, and the `Yds bad` column flags them — switch it on from Add "
            "columns to see which.", "warn"))
    return tiles, notes


def _kpi_row(flt, mode):
    key = lens.resolve((flt or {}).get("lens"))
    where = data.where_from_filters(flt)
    chips = data.chip_set(flt)
    if lens.is_scrimmage(key):
        tiles, notes = _scrim_kpis(key, where, league.resolve((flt or {}).get("league")), chips)
    else:
        tiles, notes = _kick_kpis(flt, where, league.resolve((flt or {}).get("league")), chips)
    if tiles is None:
        return dmc.Alert(f"No {lens.NOUN[key]} match these filters.", color="gray",
                         variant="light")
    if notes is None:
        notes = []
    elif not isinstance(notes, list):
        notes = [notes]
    return dmc.Stack(gap=6, children=[
        # Columns follow the tile count rather than sitting at a fixed 6. The kicks lens
        # has five tiles since the Unknown-outcome one was dropped on 2026-09-09, and a
        # six-column grid left the row stopping short with dead space on the right.
        dmc.SimpleGrid(cols={"base": 2, "sm": 3, "lg": min(len(tiles), 6)}, spacing="xs",
                       children=tiles),
        *notes,
    ])


@callback(Output("ex-kpis", "children"), Input("flt", "data"), Input("mode", "data"))
def _kpis(flt, mode):
    return _kpi_row(flt, mode or "dark")


# --------------------------------------------------------------------------- charts
@callback(
    Output("ex-c1", "figure"), Output("ex-c2", "figure"), Output("ex-c3", "figure"),
    Output("ex-c3", "style"), Output("ex-chartgrid", "cols"),
    Input("flt", "data"), Input("mode", "data"),
)
def _charts(flt, mode):
    """Every chart honours every filter, including the outcome filter. Filtering to
    one outcome does flatten the mix charts to 100%, but a chart that quietly ignored
    part of the sidebar would be worse than one that looks degenerate for an honest
    reason.

    A selection may have two charts rather than three -- `explorer_figs` returns None
    in the third slot for the multi-phase kicks view. Hiding the slot is not enough on
    its own: SimpleGrid would keep three columns and leave the two survivors at a third
    of the width each, so the column count moves with them."""
    key = lens.resolve((flt or {}).get("lens"))
    # explorer_figs takes `lg` THIRD. 7675b99 inserted it and left this call at the old
    # five-argument shape, which is not an arity error -- the chip list slid into `lg`,
    # the mode into `chips`, the height into `mode` -- so it raised deep inside
    # league.resolve ("unhashable type: 'list'") rather than here. The explorer reads the
    # header's selector, which app.py puts on the filter store.
    c1, c2, c3 = charts.explorer_figs(key, data.where_from_filters(flt),
                                      league.resolve((flt or {}).get("league")),
                                      data.chip_set(flt), mode or "dark", 320)
    if c3 is None:
        return c1, c2, charts.empty_fig(mode or "dark", height=320), \
            {"display": "none"}, {"base": 1, "lg": 2}
    return c1, c2, c3, {"height": "320px"}, {"base": 1, "lg": 3}


# --------------------------------------------------------------------------- grid chrome
@callback(
    Output("ex-cols", "data"), Output("ex-cols", "value"),
    Input("flt", "data"), Input("mode", "data"), State("ex-cols", "value"),
)
def _col_options(flt, mode, current):
    key = lens.resolve((flt or {}).get("lens"))
    opts = columns.optional_options(key, data.chip_set(flt), mode or "dark")
    valid = {o["value"] for o in opts}
    return opts, [c for c in (current or []) if c in valid]


@callback(
    Output("ex-grain", "data"), Output("ex-grain", "value"),
    Input("lens", "data"), State("ex-grain", "value"),
)
def _grain_options(key, current):
    """Grains are per lens, so a grain that does not exist on the new one has to fall
    back rather than leave the control pointing at nothing."""
    key = lens.resolve(key)
    opts = lens.GRAINS[key]
    valid = {o["value"] for o in opts}
    return opts, current if current in valid else "plays"


@callback(Output("ex-minwrap", "style"), Input("ex-grain", "value"))
def _toggle_min(grain):
    return {"display": "none"} if grain == "plays" else {}


@callback(Output("ex-min", "label"), Input("lens", "data"))
def _min_label(key):
    return f"Min {lens.NOUN[lens.resolve(key)]}"


@callback(
    Output("ex-grid-wrap", "children"), Output("ex-hint", "children"),
    Input("ex-grain", "value"), Input("mode", "data"), Input("ex-cols", "value"),
    Input("flt", "data"),
)
def _grid(grain, mode, extra, flt):
    mode = mode or "dark"
    key = lens.resolve((flt or {}).get("lens"))
    chips = data.chip_set(flt)
    if grain == "plays":
        note = ui.note("Click any row to open the play. Column headers carry their own "
                       "filters, which run in DuckDB and compose with the sidebar.",
                       "neutral")
        return ui.grid("grid-plays", mode, infinite=True, height="720px",
                       columns=columns.build(key, chips, extra, mode)), \
            _chrome(note, "ex-plays")
    if grain == "kickers":
        note = ui.note("Click a row to open that player's profile. Rates are computed "
                       "on the filtered kicks only.", "neutral")
        return ui.grid("grid-kickers", mode, height="720px",
                       columns=common.agg_cols(key, "player", chips, mode),
                       row_id="player_id"), _chrome(note, "ex-kickers")
    if lens.is_scrimmage(key):
        note = ui.note(
            "Rolled up over the filtered plays. Team and player profile pages are "
            "kicking-side only for now, so these rows do not open — the offense and "
            "defense profiles are the next pass.", "neutral")
    else:
        note = ui.note("Click a row to open that team's profile.", "neutral")
    return ui.grid("grid-teams", mode, height="720px",
                   columns=common.agg_cols(key, "team", chips, mode),
                   row_id="team_id"), _chrome(note, "ex-teams")


def _chrome(note, prefix: str):
    """The line above the grid: what a click does, and how to take the rows away.

    The download pair is rendered HERE rather than in the page's static chrome so that
    it is swapped in and out with the grid it exports -- see export.controls. The three
    grains therefore have three button pairs and three callbacks, each of which can only
    fire while its own grid is on screen.
    """
    return dmc.Group(justify="space-between", align="center", wrap="nowrap", children=[
        note, export.controls(prefix),
    ])


# --------------------------------------------------------------------------- plays grid
@callback(
    Output("grid-plays", "getRowsResponse"),
    Output("ex-plays-req", "data"),
    Input("grid-plays", "getRowsRequest"),
    State("flt", "data"),
    prevent_initial_call=True,
)
def _rows(req, flt):
    if not req:
        raise PreventUpdate
    view = data.view(flt)
    where = (f"({data.where_from_filters(flt)}) AND "
             f"({data.filter_model_to_sql(req.get('filterModel'))})")
    order = data.sort_model_to_sql(req.get("sortModel"))
    start = int(req.get("startRow") or 0)
    end = int(req.get("endRow") or (start + ui.BLOCK))
    df = data.q(f"""
        SELECT * FROM {view} WHERE {where}
        ORDER BY {order} LIMIT {max(end - start, 1)} OFFSET {start}
    """)
    # Only the two halves that survive the page: the block bounds are a scroll
    # position and would make the export a scroll position too.
    return ({"rowData": data.records(df), "rowCount": data.count_rows(view, where)},
            {"filterModel": req.get("filterModel") or {},
             "sortModel": req.get("sortModel") or []})


@callback(
    Output("row-count", "children"),
    Input("flt", "data"),
)
def _count(flt):
    """row-count sits in the always-present header, so every Input here must be
    global too -- a page-only Input would raise a nonexistent-object error on any
    other route."""
    key = lens.resolve((flt or {}).get("lens"))
    n = data.count_rows(data.view(flt), data.where_from_filters(flt))
    return f"{n:,} {lens.NOUN[key]}"


@callback(
    Output("detail-uid", "data"),
    Input("grid-plays", "selectedRows"),
    prevent_initial_call=True,
)
def _open_play(rows):
    if not rows:
        raise PreventUpdate
    return rows[0].get("play_uid")


def _floor(rows, minimum):
    """`kicks` on the kicks lens, `plays` on the others -- one control, one meaning."""
    lo = int(minimum or 1)
    return [r for r in rows
            if (r.get("kicks") if r.get("kicks") is not None else r.get("plays")
                or 0) >= lo]


@callback(
    Output("grid-kickers", "rowData"),
    Input("flt", "data"), Input("ex-min", "value"),
    prevent_initial_call=False,
)
def _kickers(flt, minimum):
    key = lens.resolve((flt or {}).get("lens"))
    if lens.is_scrimmage(key):
        return []
    return _floor(common.agg_frame(key, "player", data.where_from_filters(flt), league.resolve((flt or {}).get("league"))),
                  minimum)


@callback(
    Output("grid-teams", "rowData"),
    Input("flt", "data"), Input("ex-min", "value"),
    prevent_initial_call=False,
)
def _teams(flt, minimum):
    key = lens.resolve((flt or {}).get("lens"))
    return _floor(common.agg_frame(key, "team", data.where_from_filters(flt), league.resolve((flt or {}).get("league"))), minimum)


@callback(
    Output("url", "pathname", allow_duplicate=True),
    Input("grid-kickers", "selectedRows"),
    prevent_initial_call=True,
)
def _goto_player(rows):
    if not rows or rows[0].get("player_id") is None:
        raise PreventUpdate
    return routes.url(f"/player/{league.resolve(rows[0].get('league'))}/{int(rows[0]['player_id'])}")


@callback(
    Output("url", "pathname", allow_duplicate=True),
    Input("grid-teams", "selectedRows"),
    State("lens", "data"),
    prevent_initial_call=True,
)
def _goto_team(rows, key):
    # The team page is a kicking-side profile. Following a row into it from the
    # offense or defense lens would silently change the subject, so those rows do
    # not navigate -- the hint above the grid says so.
    if lens.is_scrimmage(lens.resolve(key)):
        raise PreventUpdate
    if not rows or rows[0].get("team_id") is None:
        raise PreventUpdate
    return routes.url(f"/team/{league.resolve(rows[0].get('league'))}/{int(rows[0]['team_id'])}")


# --------------------------------------------------------------------------- downloads
# Three callbacks rather than one, because the three grains answer the "what is on
# screen" question in genuinely different ways -- one re-runs the query, two read the
# rows the client-side grid is displaying -- and because a single callback would need
# States on all three grids, only one of which is ever in the tree.
#
# Every one of them opens on export.clicked(), which is load-bearing rather than
# defensive: these buttons arrive by callback, and Dash fires the callbacks of
# components it mounts dynamically. Without it the explorer downloaded a file on first
# paint and on every grain switch.
@callback(
    Output("ex-dl", "data"),
    Input("ex-plays-csv", "n_clicks"), Input("ex-plays-xlsx", "n_clicks"),
    State("flt", "data"), State("ex-cols", "value"), State("ex-plays-req", "data"),
    running=export.busy("ex-plays"),
    prevent_initial_call=True,
)
def _dl_plays(n_csv, n_xlsx, flt, extra, req):
    """The play grid, re-run rather than re-collected.

    The infinite row model means the browser holds a dozen blocks of a selection that
    can be 1.5M rows, so there is nothing client-side to gather. `req` is the grid's
    last getRowsRequest -- its header filters and its sort -- and composing it with the
    sidebar here is the same composition _rows does, so the file is the pane.
    """
    fmt = export.clicked(n_csv, n_xlsx)
    key = lens.resolve((flt or {}).get("lens"))
    view = data.view(flt)
    req = req or {}
    where = (f"({data.where_from_filters(flt)}) AND "
             f"({data.filter_model_to_sql(req.get('filterModel'))})")
    order = data.sort_model_to_sql(req.get("sortModel"))
    total = data.count_rows(view, where)
    # No PreventUpdate on an empty selection: DuckDB returns the schema with no rows,
    # so the file is a header row saying which columns matched nothing. A button that
    # does nothing at all is indistinguishable from a broken one.
    df = data.q(f"SELECT * FROM {view} WHERE {where} ORDER BY {order} "
                f"LIMIT {export.cap(fmt)}")
    # The grid's own columns, so the file has the set on screen including anything
    # switched on from Add columns. `mode` only decides outcome cell colours, which no
    # spreadsheet carries, so the dark catalogue is as good as either.
    cols = columns.build(key, data.chip_set(flt), extra, "dark")
    about = export.about(flt, lens.NOUN[key], grid_filters=req.get("filterModel"),
                         order=order, where=where)
    return export.deliver(fmt, export.shape(df, cols), export.stem(flt, lens.NOUN[key]),
                          about, total)


def _dl_agg(grain: str, fmt: str, rows, flt, minimum):
    """Shared body for the two rolled-up grids.

    `virtualRowData` is the client-side grid's rows after its own header filters and in
    its sort order, which is exactly the pane. It is empty for a beat after the grid
    mounts and after a filter change, so the rollup is recomputed as a fallback -- the
    same call that fills the grid, so the fallback differs from the grid only in not
    having had the header filters applied.
    """
    key = lens.resolve((flt or {}).get("lens"))
    col = "player" if grain == "kickers" else "team"
    if not rows:
        rows = _floor(common.agg_frame(key, col, data.where_from_filters(flt),
                                       league.resolve((flt or {}).get("league"))),
                      minimum)
    cols = common.agg_cols(key, col, data.chip_set(flt), "dark")
    about = export.about(flt, f"one row per {col}",
                         where=data.where_from_filters(flt))
    about.append((f"Minimum {lens.NOUN[key]}", str(int(minimum or 1))))
    return export.deliver(fmt, export.shape(rows, cols), export.stem(flt, grain), about)


@callback(
    Output("ex-dl", "data", allow_duplicate=True),
    Input("ex-kickers-csv", "n_clicks"), Input("ex-kickers-xlsx", "n_clicks"),
    State("grid-kickers", "virtualRowData"), State("flt", "data"),
    State("ex-min", "value"),
    running=export.busy("ex-kickers"),
    prevent_initial_call=True,
)
def _dl_kickers(n_csv, n_xlsx, rows, flt, minimum):
    return _dl_agg("kickers", export.clicked(n_csv, n_xlsx), rows, flt, minimum)


@callback(
    Output("ex-dl", "data", allow_duplicate=True),
    Input("ex-teams-csv", "n_clicks"), Input("ex-teams-xlsx", "n_clicks"),
    State("grid-teams", "virtualRowData"), State("flt", "data"),
    State("ex-min", "value"),
    running=export.busy("ex-teams"),
    prevent_initial_call=True,
)
def _dl_teams(n_csv, n_xlsx, rows, flt, minimum):
    return _dl_agg("teams", export.clicked(n_csv, n_xlsx), rows, flt, minimum)
