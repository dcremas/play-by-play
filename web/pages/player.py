"""The player profile.

One page per athlete, showing whichever of the three phases he actually appears in.
1,533 of the 2,729 players with five or more kicks work more than one phase and 302 do
all three, so splitting a punter/kickoff man across two pages would have hidden the
fact that they are the same person.

Only the kicking side gets a profile. Returners and tacklers are named on every play
they appear in and are searchable in the grid, but have no page of their own.
"""
from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, State, callback, dcc, html
from dash.exceptions import PreventUpdate

from .. import charts, data, lens, ui
from . import common

_ROLE_LABEL = {"field_goal": "Placekicker", "punt": "Punter", "kickoff": "Kickoff"}


def _profile(athlete_id: int) -> dict:
    df = data.q(f"""
        SELECT any_value(player) AS name,
               min(season) AS s0, max(season) AS s1,
               count(*) AS kicks,
               count(DISTINCT game_id) AS games,
               count(DISTINCT team_id) AS n_teams,
               string_agg(DISTINCT team, ' · ' ORDER BY team) AS teams,
               arg_max(team_id, season) AS last_team_id,
               arg_max(team, season) AS last_team,
               avg(player_conf) AS conf
        FROM st_play WHERE player_id = {athlete_id}
    """)
    if df.empty or not int(df.iloc[0]["kicks"] or 0):
        raise LookupError(athlete_id)
    row = df.iloc[0]
    phases = data.q(f"""
        SELECT play_kind, count(*) n FROM st_play
        WHERE player_id = {athlete_id} GROUP BY 1
    """)
    counts = dict(zip(phases["play_kind"], phases["n"].astype(int)))
    return {"name": row["name"], "s0": int(row["s0"]), "s1": int(row["s1"]),
            "kicks": int(row["kicks"]), "games": int(row["games"]),
            "teams": row["teams"], "n_teams": int(row["n_teams"]),
            "last_team": row["last_team"], "last_team_id": row["last_team_id"],
            "conf": row["conf"], "counts": counts}


def crumb(athlete_id: int):
    p = _profile(athlete_id)
    return dmc.Group(gap=6, children=[
        dmc.Text("/", size="xs", c="dimmed"),
        dmc.Text(p["name"], size="xs", fw=600),
    ])


def layout(athlete_id: int, mode: str = "dark"):
    p = _profile(athlete_id)
    present = [k for k in common.SEASON_KINDS if p["counts"].get(k)]

    role_badges = [
        dmc.Badge(f"{_ROLE_LABEL[k]} · {p['counts'][k]:,}", variant="light",
                  size="sm", radius="sm")
        for k in present
    ]
    conf = p["conf"]
    if conf is not None and conf == conf and conf < 0.999:
        role_badges.append(dmc.Badge(f"name match {conf:.2f}", variant="outline",
                                     size="sm", radius="sm"))

    return dmc.Stack(gap="md", children=[
        dcc.Store(id="ent", data={"kind": "player", "id": int(athlete_id)}),
        common.header_block(
            p["name"], role_badges,
            [f"{p['s0']}–{p['s1']}",
             f"{p['kicks']:,} kicks in {p['games']:,} games",
             p["teams"] if p["n_teams"] > 1 else None,
             f"athlete id {athlete_id}"],
            actions=dmc.Group(gap=8, children=[
                dcc.Link(dmc.Button(f"{p['last_team']} profile", variant="light",
                                    size="compact-xs"),
                         href=f"/team/{int(p['last_team_id'])}")
                if p["last_team_id"] == p["last_team_id"] else None,
                dcc.Link(dmc.Button("Back to explorer", variant="subtle",
                                    size="compact-xs"), href="/"),
            ]),
        ),
        ui.note("Every number on this page respects the sidebar filters, except the "
                "player filter itself. Narrow the seasons or the situation and the "
                "whole page follows.", "info"),
        html.Div(id="pl-kpis"),
        dmc.Divider(),
        dmc.Tabs(id="pl-tabs", value=present[0] if present else None, children=[
            dmc.TabsList([
                dmc.TabsTab(lens.PHASES["st"][k], value=k) for k in present
            ]),
            *[dmc.TabsPanel(html.Div(id=f"pl-panel-{k}"), value=k) for k in present],
        ]),
        dmc.Divider(label="Every kick", labelPosition="left"),
        common.play_grid(mode, present, height="440px"),
    ])


# --------------------------------------------------------------------------- kpis
@callback(
    Output("pl-kpis", "children"),
    Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
)
def _kpis(ent, flt, mode):
    if not ent or ent.get("kind") != "player":
        raise PreventUpdate
    where = common.entity_where(ent, flt)
    rows = common.agg_frame("st", "player", where)
    if not rows:
        return dmc.Alert("This player has no kicks under the current filters.",
                         color="gray", variant="light")
    r = rows[0]
    tiles = [ui.tile("Kicks in selection", f"{r['kicks']:,}",
                     f"{r['first_season']}–{r['last_season']}")]
    if r.get("fg_att"):
        tiles.append(ui.tile("FG make rate",
                             f"{r['fg_rate']:.1%}" if r.get("fg_rate") is not None else "—",
                             f"{int(r['fg_made'])} of {int(r['fg_att'])}"))
        tiles.append(ui.tile("Longest made",
                             f"{int(r['fg_long'])} yd" if r.get("fg_long") else "—"))
    if r.get("punts"):
        tiles.append(ui.tile("Punts", f"{int(r['punts']):,}"))
        tiles.append(ui.tile("Gross / net",
                             f"{r['punt_gross']:.1f} / {r['punt_net']:.1f}"
                             if r.get("punt_net") is not None else
                             (f"{r['punt_gross']:.1f} / —" if r.get("punt_gross") else "—"),
                             "yards"))
    if r.get("kickoffs"):
        tiles.append(ui.tile("Kickoffs", f"{int(r['kickoffs']):,}"))
        tiles.append(ui.tile("Touchback rate",
                             f"{r['tb_rate']:.1%}" if r.get("tb_rate") is not None else "—",
                             "onside excluded"))
    unk = r.get("unk_share")
    if unk:
        tiles.append(ui.tile("Unknown outcome", f"{unk:.1%}",
                             f"{int(r['unknowns']):,} kicks"))
    return dmc.SimpleGrid(cols={"base": 2, "sm": 3, "lg": 6}, spacing="xs",
                          children=tiles)


# --------------------------------------------------------------------------- phase panels
def _panel(kind: str, ent, flt, mode):
    where = common.entity_where(ent, flt)
    rows = common.season_rows(kind, where)
    if not rows:
        return dmc.Alert(f"No {lens.PHASES['st'][kind].lower()} plays under these filters.",
                         color="gray", variant="light")
    caveat = None
    if kind in ("punt", "kickoff"):
        worst = max((r.get("unk_share") or 0) for r in rows)
        if worst > 0:
            caveat = ui.note(
                "The Unknown column is kicks whose outcome the play text does not "
                "state. Outcome counts and rates in a season with a high Unknown share "
                f"are understated by that much — worst season here is {worst:.0%}.",
                "warn")
    return dmc.Stack(gap="sm", mt="sm", children=[
        dmc.SimpleGrid(cols={"base": 1, "lg": 2}, spacing="sm", children=[
            ui.graph(f"pl-trend-{kind}", 320),
            ui.graph(f"pl-mix-{kind}", 320),
        ]),
        ui.section("By season", "the same rows the charts are drawn from"),
        caveat,
        ui.grid(f"pl-seasons-{kind}", mode, rows=rows,
                columns=common.season_cols(kind), height="300px", row_id="season"),
    ])


def _register_panel(kind: str):
    @callback(
        Output(f"pl-panel-{kind}", "children"),
        Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
        prevent_initial_call=False,
    )
    def _cb(ent, flt, mode, _kind=kind):
        if not ent or ent.get("kind") != "player":
            raise PreventUpdate
        return _panel(_kind, ent, flt, mode or "dark")

    @callback(
        Output(f"pl-trend-{kind}", "figure"), Output(f"pl-mix-{kind}", "figure"),
        Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
        prevent_initial_call=False,
    )
    def _figs(ent, flt, mode, _kind=kind):
        if not ent or ent.get("kind") != "player":
            raise PreventUpdate
        mode = mode or "dark"
        where = common.entity_where(ent, flt)
        scoped = f"{where} AND play_kind = '{_kind}'"
        return (charts.season_trend(where, mode, _kind, 320),
                charts.outcome_by_distance(scoped, [_kind], mode, 320))


for _k in common.SEASON_KINDS:
    _register_panel(_k)
