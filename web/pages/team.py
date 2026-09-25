"""The team profile: a decade roll-up on top, one row per season underneath.

Conference is read per season off dim_team_season at snapshot build time, so a
program that changed leagues mid-decade shows the conference it was actually in that
year rather than the one it is in now.
"""
from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, callback, dcc, html
from dash.exceptions import PreventUpdate

from .. import charts, data, league, lens, ui
from . import common


def _profile(team_id: int, lg: str) -> dict:
    """One team in one league.

    The league is not optional and is not a filter the reader chose. Team ids collide
    across the two corpora -- 2 is Auburn and also the Buffalo Bills -- so a profile that
    queried on team_id alone would union two franchises into one page and report their
    combined kick count under whichever name won the any_value().
    """
    lg = league.resolve(lg)
    df = data.q(f"""
        SELECT any_value(team) AS name,
               min(season) AS s0, max(season) AS s1,
               count(*) AS kicks,
               count(DISTINCT game_id) AS games,
               count(DISTINCT player_id) AS kickers,
               arg_max(conference, season) AS conference,
               arg_max(COALESCE(nfl_division, ncaa_division), season) AS division
        FROM st_play WHERE team_id = {team_id} AND league = {data.lit(lg)}
    """)
    if df.empty or not int(df.iloc[0]["kicks"] or 0):
        raise LookupError(team_id)
    r = df.iloc[0]
    return {"name": r["name"], "league": lg, "s0": int(r["s0"]), "s1": int(r["s1"]),
            "kicks": int(r["kicks"]), "games": int(r["games"]),
            "kickers": int(r["kickers"]), "conference": r["conference"],
            "division": r["division"]}


def crumb(team_id: int, lg: str = league.DEFAULT):
    p = _profile(team_id, lg)
    return dmc.Group(gap=6, children=[
        dmc.Text("/", size="xs", c="dimmed"),
        dmc.Text(p["name"], size="xs", fw=600),
    ])


def layout(team_id: int, lg: str = league.DEFAULT, mode: str = "dark"):
    lg = league.resolve(lg)
    p = _profile(team_id, lg)
    conf_hist = data.q(f"""
        SELECT DISTINCT season, conference FROM st_play
        WHERE team_id = {team_id} AND league = {data.lit(lg)} ORDER BY season
    """)
    moved = conf_hist["conference"].nunique() > 1

    return dmc.Stack(gap="md", children=[
        dcc.Store(id="ent", data={"kind": "team", "id": int(team_id), "league": lg}),
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
        dmc.SimpleGrid(id="tm-chartgrid", cols={"base": 1, "lg": 3}, spacing="sm",
                       children=[ui.graph("tm-c1", 330), ui.graph("tm-c2", 330),
                                 ui.graph("tm-c3", 330)]),
        dmc.Divider(label="By season", labelPosition="left"),
        dmc.Tabs(id="tm-tabs", value="field_goal", children=[
            dmc.TabsList([dmc.TabsTab(lens.PHASES["st"][k], value=k)
                          for k in common.SEASON_KINDS]),
            *[dmc.TabsPanel(html.Div(id=f"tm-panel-{k}"), value=k)
              for k in common.SEASON_KINDS],
        ]),
        dmc.Divider(label="Who kicked", labelPosition="left"),
        ui.note("Click a row to open that player's profile.", "neutral"),
        html.Div(id="tm-people"),
        dmc.Divider(label="Every kick", labelPosition="left"),
        common.play_grid(mode, common.SEASON_KINDS, height="440px"),
    ])


# --------------------------------------------------------------------------- roll-up
@callback(
    Output("tm-kpis", "children"),
    Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
)
def _kpis(ent, flt, mode):
    if not ent or ent.get("kind") != "team":
        raise PreventUpdate
    rows = common.agg_frame("st", "team", common.entity_where(ent, flt))
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
        # Two rates, never one. A programme that goes for two often would otherwise
        # look like it cannot kick.
        ui.tile("XP / 2pt rate",
                " / ".join(
                    f"{r[k]:.0%}" if r.get(k) is not None else "—"
                    for k in ("pat_rate", "two_rate")),
                f"{int(r.get('pat_att') or 0):,} XP · "
                f"{int(r.get('two_att') or 0):,} two-point"),
    ]
    # The Unknown-outcome tile was dropped here on 2026-09-09 alongside the explorer's.
    # The Unknown COLUMN stays in the by-season grids below, and so does the note that
    # explains it -- that grid is the detail this page exists for.
    return dmc.SimpleGrid(cols={"base": 2, "sm": 3, "lg": min(len(tiles), 6)},
                          spacing="xs", children=tiles)


@callback(
    Output("tm-c1", "figure"), Output("tm-c2", "figure"), Output("tm-c3", "figure"),
    Output("tm-c3", "style"), Output("tm-chartgrid", "cols"),
    Input("ent", "data"), Input("flt", "data"), Input("mode", "data"),
)
def _charts(ent, flt, mode):
    if not ent or ent.get("kind") != "team":
        raise PreventUpdate
    mode = mode or "dark"
    where = common.entity_where(ent, flt)
    ps = common.st_chips(flt)
    # Same two-or-three shape as the explorer: `explorer_figs` returns None in the
    # third slot for the multi-phase kicks view, which is this page's default. Hide the
    # slot AND drop the column count, or the two survivors keep a third of the width.
    c1, c2, c3 = charts.explorer_figs("st", where, ps, mode, 330)
    if c3 is None:
        return c1, c2, charts.empty_fig(mode, height=330), \
            {"display": "none"}, {"base": 1, "lg": 2}
    return c1, c2, c3, {"height": "330px"}, {"base": 1, "lg": 3}


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
            return common.empty_panel(_kind, flt)
        caveats = []
        live = {r["season"] for r in data.in_progress_seasons()}
        hit = sorted(live.intersection(r["season"] for r in rows))
        if hit:
            caveats.append(ui.note(
                f"{', '.join(str(y) for y in hit)} is still being played, so its row is a "
                f"part-season sitting next to full ones. Totals are not comparable; rates "
                f"are, on a much smaller sample.", "info"))
        caveat = None
        if _kind == "conversion":
            caveat = common.conversion_caveat(rows)
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
                    columns=common.season_cols(_kind, "team"), height="320px",
                    row_id="season"),
        ])


for _k in common.SEASON_KINDS:
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
    rows = common.agg_frame("st", "player", common.entity_where(ent, flt))
    ps = common.st_chips(flt)
    # Its own id, not the explorer's `grid-kickers`: sharing the id would have been
    # free row-click navigation, but it also pulls in the explorer's rowData callback,
    # whose `ex-min` Input does not exist on this page.
    return ui.grid("grid-tm-kickers", mode, rows=rows,
                   columns=common.agg_cols("st", "player", ps, mode),
                   height="340px", row_id="player_id")


@callback(
    Output("url", "pathname", allow_duplicate=True),
    Input("grid-tm-kickers", "selectedRows"),
    prevent_initial_call=True,
)
def _goto_player(rows):
    if not rows or rows[0].get("player_id") is None:
        raise PreventUpdate
    # League-qualified: an athlete id alone is not enough to name a career, because the
    # same id is one man's college AND pro plays.
    lg = league.resolve(rows[0].get("league"))
    return f"/player/{lg}/{int(rows[0]['player_id'])}"
