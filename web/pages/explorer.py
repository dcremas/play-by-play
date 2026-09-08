"""The explorer: one filter set, three grains.

Plays is the point of the app -- 243k rows on AG Grid's infinite row model, so
sorting and per-column filtering are pushed down into DuckDB and the browser never
holds more than a dozen blocks. Kickers and Teams are the same selection rolled up,
and clicking a row on either navigates to that entity's profile.
"""
from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, State, callback, html, no_update
from dash.exceptions import PreventUpdate

from .. import charts, columns, data, ui
from . import common

GRAINS = [{"value": "plays", "label": "Plays"},
          {"value": "kickers", "label": "Kickers & punters"},
          {"value": "teams", "label": "Teams"}]


def layout(mode: str = "dark"):
    return dmc.Stack(gap="md", children=[
        html.Div(id="ex-kpis"),
        dmc.Accordion(
            value="shape", variant="separated", radius="sm", chevronPosition="right",
            children=[dmc.AccordionItem(value="shape", children=[
                dmc.AccordionControl(dmc.Text("Shape of the current selection", size="xs",
                                              fw=600)),
                dmc.AccordionPanel(dmc.SimpleGrid(
                    cols={"base": 1, "lg": 3}, spacing="sm",
                    children=[ui.graph("ex-c1", 340), ui.graph("ex-c2", 340),
                              ui.graph("ex-c3", 340)],
                )),
            ])],
        ),
        dmc.Group(justify="space-between", align="flex-end", children=[
            dmc.SegmentedControl(id="ex-grain", value="plays", data=GRAINS, size="xs"),
            dmc.Group(gap="sm", align="flex-end", children=[
                html.Div(id="ex-minwrap", children=dmc.NumberInput(
                    id="ex-min", label="Min kicks", value=10, min=1, max=500, step=5,
                    size="xs", w=110)),
                dmc.MultiSelect(id="ex-cols", data=[], value=[], size="xs", w=290,
                                searchable=True, clearable=True,
                                label="Add columns", placeholder="Default set"),
            ]),
        ]),
        html.Div(id="ex-hint"),
        html.Div(id="ex-grid-wrap"),
    ])


# --------------------------------------------------------------------------- kpis
def _kpi_row(flt, mode):
    where = data.where_from_filters(flt)
    ps = data.phase_set(flt)
    key = columns.key_for(list(ps))
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
               avg(punt_net_yds) net,
               sum(CASE WHEN outcome = 'Touchback' THEN 1 ELSE 0 END) tb,
               sum(CASE WHEN play_kind = 'kickoff' AND NOT COALESCE(onside, FALSE)
                        THEN 1 ELSE 0 END) ko,
               count(player_id) linked
        FROM st WHERE {where}
    """).iloc[0]

    n = int(r.n)
    if not n:
        return dmc.Alert("No kicks match these filters.", color="gray", variant="light")

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
    else:
        tiles.append(ui.tile("Phases", f"{len(ps)}",
                             ", ".join(data.PHASES[p] for p in data.PHASE_ORDER
                                       if p in ps)))

    unk = int(r.unk)
    tiles.append(ui.tile("Unknown outcome", f"{unk / n:.1%}" if unk else "0%",
                         f"{unk:,} kicks"))

    out = [dmc.SimpleGrid(cols={"base": 2, "sm": 3, "lg": 6}, spacing="xs",
                          children=tiles)]
    if unk:
        out.append(ui.note(
            f"{unk:,} of these {n:,} kicks ({unk / n:.1%}) state no outcome anywhere in the "
            "play text — ESPN's terse form names none, and no regex recovers what is not "
            "written. The warehouse records that as NULL rather than false, so they are "
            "shown as Unknown here instead of being folded into the real outcomes. Nothing "
            "on this page is understated by them; what they cost is denominator. Worst in "
            "2023–2025.", "warn"))
    return dmc.Stack(gap=6, children=out)


@callback(Output("ex-kpis", "children"), Input("flt", "data"), Input("mode", "data"))
def _kpis(flt, mode):
    return _kpi_row(flt, mode or "dark")


# --------------------------------------------------------------------------- charts
@callback(
    Output("ex-c1", "figure"), Output("ex-c2", "figure"), Output("ex-c3", "figure"),
    Input("flt", "data"), Input("mode", "data"),
)
def _charts(flt, mode):
    """Every chart honours every filter, including the outcome filter. Filtering to
    one outcome does flatten the mix charts to 100%, but a chart that quietly ignored
    part of the sidebar would be worse than one that looks degenerate for an honest
    reason."""
    mode = mode or "dark"
    ps = sorted(data.phase_set(flt))
    return charts.explorer_figs(data.where_from_filters(flt), ps, mode, 340)


# --------------------------------------------------------------------------- grid chrome
@callback(
    Output("ex-cols", "data"), Output("ex-cols", "value"),
    Input("flt", "data"), Input("mode", "data"), State("ex-cols", "value"),
)
def _col_options(flt, mode, current):
    opts = columns.optional_options(sorted(data.phase_set(flt)), mode or "dark")
    valid = {o["value"] for o in opts}
    return opts, [c for c in (current or []) if c in valid]


@callback(Output("ex-minwrap", "style"), Input("ex-grain", "value"))
def _toggle_min(grain):
    return {"display": "none"} if grain == "plays" else {}


@callback(
    Output("ex-grid-wrap", "children"), Output("ex-hint", "children"),
    Input("ex-grain", "value"), Input("mode", "data"), Input("ex-cols", "value"),
    Input("flt", "data"),
)
def _grid(grain, mode, extra, flt):
    mode = mode or "dark"
    ps = sorted(data.phase_set(flt))
    if grain == "plays":
        hint = ui.note("Click any row to open the play. Column headers carry their own "
                       "filters, which run in DuckDB and compose with the sidebar.",
                       "neutral")
        return ui.grid("grid-plays", mode, infinite=True, height="620px",
                       columns=columns.build(ps, extra, mode)), hint
    if grain == "kickers":
        hint = ui.note("Click a row to open that player's profile. Rates are computed "
                       "on the filtered kicks only.", "neutral")
        return ui.grid("grid-kickers", mode, height="620px",
                       columns=common.agg_cols("player", ps, mode), row_id="player_id"), hint
    hint = ui.note("Click a row to open that team's profile.", "neutral")
    return ui.grid("grid-teams", mode, height="620px",
                   columns=common.agg_cols("team", ps, mode), row_id="team_id"), hint


# --------------------------------------------------------------------------- plays grid
@callback(
    Output("grid-plays", "getRowsResponse"),
    Input("grid-plays", "getRowsRequest"),
    State("flt", "data"),
    prevent_initial_call=True,
)
def _rows(req, flt):
    if not req:
        raise PreventUpdate
    where = (f"({data.where_from_filters(flt)}) AND "
             f"({data.filter_model_to_sql(req.get('filterModel'))})")
    order = data.sort_model_to_sql(req.get("sortModel"))
    start = int(req.get("startRow") or 0)
    end = int(req.get("endRow") or (start + ui.BLOCK))
    df = data.q(f"""
        SELECT * FROM st WHERE {where}
        ORDER BY {order} LIMIT {max(end - start, 1)} OFFSET {start}
    """)
    return {"rowData": data.records(df), "rowCount": data.count_rows(where)}


@callback(
    Output("row-count", "children"),
    Input("flt", "data"),
)
def _count(flt):
    """row-count sits in the always-present header, so every Input here must be
    global too -- a page-only Input would raise a nonexistent-object error on any
    other route."""
    n = data.count_rows(data.where_from_filters(flt))
    return f"{n:,} kicks selected"


@callback(
    Output("detail-uid", "data"),
    Input("grid-plays", "selectedRows"),
    prevent_initial_call=True,
)
def _open_play(rows):
    if not rows:
        raise PreventUpdate
    return rows[0].get("play_uid")


@callback(
    Output("grid-kickers", "rowData"),
    Input("flt", "data"), Input("ex-min", "value"),
    prevent_initial_call=False,
)
def _kickers(flt, minimum):
    rows = common.agg_frame("player", data.where_from_filters(flt))
    lo = int(minimum or 1)
    return [r for r in rows if (r.get("kicks") or 0) >= lo]


@callback(
    Output("grid-teams", "rowData"),
    Input("flt", "data"), Input("ex-min", "value"),
    prevent_initial_call=False,
)
def _teams(flt, minimum):
    rows = common.agg_frame("team", data.where_from_filters(flt))
    lo = int(minimum or 1)
    return [r for r in rows if (r.get("kicks") or 0) >= lo]


@callback(
    Output("url", "pathname", allow_duplicate=True),
    Input("grid-kickers", "selectedRows"),
    prevent_initial_call=True,
)
def _goto_player(rows):
    if not rows or rows[0].get("player_id") is None:
        raise PreventUpdate
    return f"/player/{int(rows[0]['player_id'])}"


@callback(
    Output("url", "pathname", allow_duplicate=True),
    Input("grid-teams", "selectedRows"),
    prevent_initial_call=True,
)
def _goto_team(rows):
    if not rows or rows[0].get("team_id") is None:
        raise PreventUpdate
    return f"/team/{int(rows[0]['team_id'])}"
