"""The team profile: a decade roll-up on top, one row per season underneath.

Conference is read per season off dim_team_season at snapshot build time, so a
program that changed leagues mid-decade shows the conference it was actually in that
year rather than the one it is in now.
"""
from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, callback, dcc, html
from dash.exceptions import PreventUpdate

from .. import charts, data, ui
from . import common


def _profile(team_id: int) -> dict:
    df = data.q(f"""
        SELECT any_value(team) AS name,
               min(season) AS s0, max(season) AS s1,
               count(*) AS kicks,
               count(DISTINCT game_id) AS games,
               count(DISTINCT player_id) AS kickers,
               arg_max(conference, season) AS conference,
               arg_max(division, season) AS division
        FROM st WHERE team_id = {team_id}
    """)
    if df.empty or not int(df.iloc[0]["kicks"] or 0):
        raise LookupError(team_id)
    r = df.iloc[0]
    return {"name": r["name"], "s0": int(r["s0"]), "s1": int(r["s1"]),
            "kicks": int(r["kicks"]), "games": int(r["games"]),
            "kickers": int(r["kickers"]), "conference": r["conference"],
            "division": r["division"]}


def crumb(team_id: int):
    p = _profile(team_id)
    return dmc.Group(gap=6, children=[
        dmc.Text("/", size="xs", c="dimmed"),
        dmc.Text(p["name"], size="xs", fw=600),
    ])


def layout(team_id: int, mode: str = "dark"):
    p = _profile(team_id)
    conf_hist = data.q(f"""
        SELECT DISTINCT season, conference FROM st
        WHERE team_id = {team_id} ORDER BY season
    """)
    moved = conf_hist["conference"].nunique() > 1

    return dmc.Stack(gap="md", children=[
        dcc.Store(id="ent", data={"kind": "team", "id": int(team_id)}),
        common.header_block(
            p["name"],
            [dmc.Badge(p["conference"] or "—", variant="light", size="sm", radius="sm"),
             dmc.Badge(p["division"] or "—", variant="outline", size="sm", radius="sm")],
            [f"{p['s0']}–{p['s1']}",
             f"{p['kicks']:,} kicks in {p['games']:,} games",
             f"{p['kickers']:,} kickers and punters"],
            actions=dcc.Link(dmc.Button("Back to explorer", variant="subtle",
                                        size="compact-xs"), href="/"),
        ),
        ui.note(
            ("This program changed conference inside the window — "
             + ", ".join(f"{int(r.season)} {r.conference}"
                         for r in conf_hist.itertuples())
             + ". Conference is joined per (team, season), so each row carries the "
               "league it was actually in.") if moved else
            "Every number on this page respects the sidebar filters, except the team "
            "filter itself.", "info" if not moved else "warn"),
        html.Div(id="tm-kpis"),
        dmc.Divider(label="Across the whole window", labelPosition="left"),
        dmc.SimpleGrid(cols={"base": 1, "lg": 3}, spacing="sm", children=[
            ui.graph("tm-c1", 330), ui.graph("tm-c2", 330), ui.graph("tm-c3", 330),
        ]),
        dmc.Divider(label="By season", labelPosition="left"),
        dmc.Tabs(id="tm-tabs", value="field_goal", children=[
            dmc.TabsList([dmc.TabsTab(data.PHASES[k], value=k)
                          for k in data.PHASE_ORDER]),
            *[dmc.TabsPanel(html.Div(id=f"tm-panel-{k}"), value=k)
              for k in data.PHASE_ORDER],
        ]),
        dmc.Divider(label="Who kicked", labelPosition="left"),
        ui.note("Click a row to open that player's profile.", "neutral"),
        html.Div(id="tm-people"),
        dmc.Divider(label="Every kick", labelPosition="left"),
        common.play_grid(mode, data.PHASE_ORDER, height="440px"),
    ])


# --------------------------------------------------------------------------- roll-up
@callback(
    Output("tm-kpis", "children"),
    Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
)
def _kpis(ent, flt, mode):
    if not ent or ent.get("kind") != "team":
        raise PreventUpdate
    rows = common.agg_frame("team", common.entity_where(ent, flt))
    if not rows:
        return dmc.Alert("No kicks for this team under the current filters.",
                         color="gray", variant="light")
    r = rows[0]
    tiles = [
        ui.tile("Kicks in selection", f"{r['kicks']:,}",
                f"{r['first_season']}–{r['last_season']}"),
        ui.tile("Kickers & punters", f"{int(r['kickers']):,}"),
        ui.tile("FG make rate",
                f"{r['fg_rate']:.1%}" if r.get("fg_rate") is not None else "—",
                f"{int(r['fg_made'] or 0)} of {int(r['fg_att'] or 0)}"),
        ui.tile("Punt gross / net",
                f"{r['punt_gross']:.1f} / {r['punt_net']:.1f}"
                if r.get("punt_net") is not None else "—", "yards"),
        ui.tile("Touchback rate",
                f"{r['tb_rate']:.1%}" if r.get("tb_rate") is not None else "—",
                "onside excluded"),
        ui.tile("Unknown outcome",
                f"{r['unk_share']:.1%}" if r.get("unk_share") is not None else "—",
                f"{int(r['unknowns'] or 0):,} kicks"),
    ]
    return dmc.SimpleGrid(cols={"base": 2, "sm": 3, "lg": 6}, spacing="xs",
                          children=tiles)


@callback(
    Output("tm-c1", "figure"), Output("tm-c2", "figure"), Output("tm-c3", "figure"),
    Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
)
def _charts(ent, flt, mode):
    if not ent or ent.get("kind") != "team":
        raise PreventUpdate
    mode = mode or "dark"
    where = common.entity_where(ent, flt)
    ps = sorted(data.phase_set(flt))
    return charts.explorer_figs(where, ps, mode, 330)


# --------------------------------------------------------------------------- panels
def _register_panel(kind: str):
    @callback(
        Output(f"tm-panel-{kind}", "children"),
        Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
        prevent_initial_call=False,
    )
    def _cb(ent, flt, mode, _kind=kind):
        if not ent or ent.get("kind") != "team":
            raise PreventUpdate
        rows = common.season_rows(_kind, common.entity_where(ent, flt))
        if not rows:
            return dmc.Alert(
                f"No {data.PHASES[_kind].lower()} plays under these filters.",
                color="gray", variant="light")
        caveats = []
        live = {r["season"] for r in data.in_progress_seasons()}
        hit = sorted(live.intersection(r["season"] for r in rows))
        if hit:
            caveats.append(ui.note(
                f"{', '.join(str(y) for y in hit)} is still being played, so its row is a "
                f"part-season sitting next to full ones. Totals are not comparable; rates "
                f"are, on a much smaller sample.", "info"))
        caveat = None
        if _kind in ("punt", "kickoff"):
            worst = max((r.get("unk_share") or 0) for r in rows)
            if worst > 0:
                caveat = ui.note(
                    "Unknown is kicks whose outcome the play text does not state; the "
                    "outcome counts beside it are understated by that much. Worst "
                    f"season here is {worst:.0%}.", "warn")
        return dmc.Stack(gap="sm", mt="sm", children=[
            *caveats, caveat,
            ui.grid(f"tm-seasons-{_kind}", mode or "dark", rows=rows,
                    columns=common.season_cols(_kind), height="320px",
                    row_id="season"),
        ])


for _k in data.PHASE_ORDER:
    _register_panel(_k)


# --------------------------------------------------------------------------- personnel
@callback(
    Output("tm-people", "children"),
    Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
)
def _people(ent, flt, mode):
    if not ent or ent.get("kind") != "team":
        raise PreventUpdate
    mode = mode or "dark"
    rows = common.agg_frame("player", common.entity_where(ent, flt))
    ps = sorted(data.phase_set(flt))
    # Its own id, not the explorer's `grid-kickers`: sharing the id would have been
    # free row-click navigation, but it also pulls in the explorer's rowData callback,
    # whose `ex-min` Input does not exist on this page.
    return ui.grid("grid-tm-kickers", mode, rows=rows,
                   columns=common.agg_cols("player", ps, mode),
                   height="340px", row_id="player_id")


@callback(
    Output("url", "pathname", allow_duplicate=True),
    Input("grid-tm-kickers", "selectedRows"),
    prevent_initial_call=True,
)
def _goto_player(rows):
    if not rows or rows[0].get("player_id") is None:
        raise PreventUpdate
    return f"/player/{int(rows[0]['player_id'])}"
