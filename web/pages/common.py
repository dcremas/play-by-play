"""Pieces shared by the explorer and the two profile pages.

Aggregates live here so a leaderboard, a team's personnel list and a player's
season table are all computed the same way and cannot drift apart.

There are two measure vocabularies, one per fact. The kicks roll up to make rates and
net yards; the scrimmage plays roll up to yards per play, first-down rate and
turnovers. Offense and defense share the second one exactly -- the same expressions
over the same rows, read from opposite ends -- and differ only in the column headers,
which is the whole reason they are one set of SQL and two label tables.
"""
from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, State, callback, html
from dash.exceptions import PreventUpdate

from .. import columns, data, lens, ui

# --------------------------------------------------------------------------- measures
METRICS = """
    count(*) AS kicks,
    min(season) AS first_season,
    max(season) AS last_season,
    count(DISTINCT game_id) AS games,
    sum(CASE WHEN play_kind = 'field_goal' AND outcome <> 'Negated'
             THEN 1 ELSE 0 END) AS fg_att,
    sum(CASE WHEN outcome = 'Made' THEN 1 ELSE 0 END) AS fg_made,
    max(CASE WHEN outcome = 'Made' THEN fg_distance_yds END) AS fg_long,
    count(*) FILTER (play_kind = 'punt') AS punts,
    avg(punt_gross_yds) AS punt_gross,
    avg(punt_net_yds) AS punt_net,
    count(*) FILTER (play_kind = 'kickoff') AS kickoffs,
    avg(kickoff_yds) AS ko_dist,
    count(*) FILTER (play_kind = 'kickoff' AND NOT COALESCE(onside, FALSE)) AS ko_denom,
    sum(CASE WHEN outcome = 'Touchback' THEN 1 ELSE 0 END) AS touchbacks,
    sum(CASE WHEN outcome = 'Unknown' THEN 1 ELSE 0 END) AS unknowns
"""

DERIVED = """
    fg_made::DOUBLE / nullif(fg_att, 0)      AS fg_rate,
    touchbacks::DOUBLE / nullif(ko_denom, 0) AS tb_rate,
    unknowns::DOUBLE / nullif(kicks, 0)      AS unk_share
"""

# Scrimmage measures. `snaps` is the denominator for anything per-play: it excludes
# penalties, whose statYardage is a penalty distance rather than a gain, and `ypp`
# excludes turnovers on top of that, because ESPN records the defense's return there
# and not the offense's gain.
SCRIM_METRICS = """
    count(*)                                                       AS plays,
    min(season)                                                     AS first_season,
    max(season)                                                     AS last_season,
    count(DISTINCT game_id)                                         AS games,
    count(*) FILTER (play_kind IN ('rush', 'pass', 'sack'))          AS snaps,
    avg(yards_gained) FILTER (play_kind IN ('rush', 'pass', 'sack')
                              AND NOT COALESCE(is_turnover, FALSE)) AS ypp,
    sum(yards_gained) FILTER (play_kind IN ('rush', 'pass', 'sack')
                              AND NOT COALESCE(is_turnover, FALSE)) AS yards,
    count(*) FILTER (COALESCE(first_down_gained, FALSE))             AS first_downs,
    count(*) FILTER (COALESCE(is_touchdown, FALSE))                  AS tds,
    count(*) FILTER (COALESCE(is_turnover, FALSE))                   AS turnovers,
    count(*) FILTER (play_kind = 'sack')                             AS sacks,
    count(*) FILTER (play_kind = 'rush')                             AS rushes,
    count(*) FILTER (play_kind = 'pass')                             AS passes,
    count(*) FILTER (COALESCE(is_complete, FALSE))                   AS completions,
    count(*) FILTER (play_kind = 'penalty')                          AS penalties
"""

SCRIM_DERIVED = """
    first_downs::DOUBLE / nullif(snaps, 0)   AS fd_rate,
    completions::DOUBLE / nullif(passes, 0)  AS comp_rate,
    turnovers::DOUBLE / nullif(snaps, 0)     AS to_rate
"""

# One event, two readings. Only the words change.
SCRIM_HEADERS = {
    "off": {"ypp": "Yds/play", "yards": "Yards", "tds": "TD", "fd_rate": "1st down %",
            "turnovers": "Turnovers", "to_rate": "TO rate", "sacks": "Sacks taken"},
    "def": {"ypp": "Yds/play allowed", "yards": "Yards allowed", "tds": "TD allowed",
            "fd_rate": "1st down % allowed", "turnovers": "Takeaways",
            "to_rate": "Takeaway rate", "sacks": "Sacks made"},
}

PCT = "params.value == null ? '' : d3.format('.1%')(params.value)"
ONE = "params.value == null ? '' : d3.format('.1f')(params.value)"
COM = "params.value == null ? '' : d3.format(',')(params.value)"


def num(field, header, width=92, fmt=None):
    d = {"field": field, "headerName": header, "filter": "agNumberColumnFilter",
         "minWidth": width, "type": "rightAligned", "cellClass": "num-cell"}
    if fmt:
        d["valueFormatter"] = {"function": fmt}
    return d


def txt(field, header, width=150, pinned=None):
    d = {"field": field, "headerName": header, "filter": "agTextColumnFilter",
         "minWidth": width}
    if pinned:
        d["pinned"] = pinned
        d["cellClass"] = "name-cell"
    return d


# --------------------------------------------------------------------------- aggregates
def agg_frame(key: str, grain: str, where: str) -> list[dict]:
    """One row per player or per team over the given selection."""
    key = lens.resolve(key)
    if lens.is_scrimmage(key):
        return _scrim_agg_frame(key, grain, where)
    if grain == "player":
        col, name_expr = "player_id", "any_value(player) AS player"
        extra = """,
            string_agg(DISTINCT team, ' · ' ORDER BY team) AS teams,
            avg(player_conf) AS name_conf"""
        guard = "player_id IS NOT NULL"
    else:
        col, name_expr = "team_id", "any_value(team) AS team"
        # arg_max, not any_value: a team's conference is a function of the season, so
        # the label has to be the most recent one inside the selection or realignment
        # silently mislabels the row.
        extra = """,
            count(DISTINCT player_id) AS kickers,
            arg_max(conference, season) AS conference"""
        guard = "team_id IS NOT NULL"
    df = data.q(f"""
        WITH agg AS (
            SELECT {col}, {name_expr}, {METRICS} {extra}
            FROM st_play WHERE {where} AND {guard} GROUP BY {col}
        )
        SELECT *, {DERIVED} FROM agg ORDER BY kicks DESC
    """)
    return data.records(df)


def _scrim_agg_frame(key: str, grain: str, where: str) -> list[dict]:
    """Team rollups on a scrimmage lens. Player grain is not offered yet -- see
    lens.GRAINS for why -- so anything but `team` comes back empty rather than
    guessing a role."""
    if grain != "team":
        return []
    df = data.q(f"""
        WITH agg AS (
            SELECT team_id, any_value(team) AS team, {SCRIM_METRICS},
                   arg_max(conference, season) AS conference
            FROM {lens.VIEW[key]} WHERE {where} AND team_id IS NOT NULL
            GROUP BY team_id
        )
        SELECT *, {SCRIM_DERIVED} FROM agg ORDER BY plays DESC
    """)
    return data.records(df)


def agg_cols(key: str, grain: str, chips, mode: str = "dark") -> list[dict]:
    key = lens.resolve(key)
    if lens.is_scrimmage(key):
        h = SCRIM_HEADERS[key]
        return [txt("team", lens.SUBJECT[key], 185, "left"),
                txt("conference", "Conference", 155),
                num("plays", "Plays", 90, COM), num("games", "Games", 88, COM),
                num("first_season", "From", 80), num("last_season", "To", 74),
                num("ypp", h["ypp"], 118, ONE), num("yards", h["yards"], 108, COM),
                num("fd_rate", h["fd_rate"], 128, PCT),
                num("tds", h["tds"], 92, COM),
                num("turnovers", h["turnovers"], 108, COM),
                num("to_rate", h["to_rate"], 108, PCT),
                num("comp_rate", "Comp %", 96, PCT),
                num("sacks", h["sacks"], 112, COM),
                num("rushes", "Rushes", 92, COM), num("passes", "Passes", 92, COM),
                num("penalties", "Penalties", 100, COM)]
    ps = set(chips)
    if grain == "player":
        cols = [txt("player", "Player", 165, "left"), txt("teams", "Team(s)", 190)]
    else:
        cols = [txt("team", "Team", 185, "left"), txt("conference", "Conference", 155),
                num("kickers", "Kickers", 92, COM)]
    cols += [num("kicks", "Kicks", 88, COM), num("first_season", "From", 80),
             num("last_season", "To", 74)]
    if "field_goal" in ps:
        cols += [num("fg_att", "FG att", 88, COM), num("fg_made", "FG made", 94, COM),
                 num("fg_rate", "FG rate", 92, PCT), num("fg_long", "Longest", 96)]
    if "punt" in ps:
        cols += [num("punts", "Punts", 88, COM), num("punt_gross", "Gross", 88, ONE),
                 num("punt_net", "Net", 82, ONE)]
    if "kickoff" in ps:
        cols += [num("kickoffs", "KOs", 82, COM), num("ko_dist", "KO dist", 92, ONE),
                 num("tb_rate", "TB rate", 92, PCT)]
    cols.append(num("unk_share", "Unknown", 100, PCT))
    return cols


# --------------------------------------------------------------------------- season tables
_SEASON_SQL = {
    "field_goal": ("""
        SELECT season,
               count(*) FILTER (outcome <> 'Negated')                     AS att,
               sum(CASE WHEN outcome = 'Made' THEN 1 ELSE 0 END)          AS made,
               max(CASE WHEN outcome = 'Made' THEN fg_distance_yds END)   AS longest,
               avg(fg_distance_yds)                                       AS avg_dist,
               count(*) FILTER (outcome = 'Blocked')                      AS blocked,
               count(*) FILTER (fg_distance_yds < 30 AND outcome <> 'Negated') AS a_u30,
               count(*) FILTER (fg_distance_yds < 30 AND outcome = 'Made')     AS m_u30,
               count(*) FILTER (fg_distance_yds BETWEEN 30 AND 39
                                AND outcome <> 'Negated')                 AS a_30,
               count(*) FILTER (fg_distance_yds BETWEEN 30 AND 39
                                AND outcome = 'Made')                     AS m_30,
               count(*) FILTER (fg_distance_yds BETWEEN 40 AND 49
                                AND outcome <> 'Negated')                 AS a_40,
               count(*) FILTER (fg_distance_yds BETWEEN 40 AND 49
                                AND outcome = 'Made')                     AS m_40,
               count(*) FILTER (fg_distance_yds >= 50 AND outcome <> 'Negated') AS a_50,
               count(*) FILTER (fg_distance_yds >= 50 AND outcome = 'Made')     AS m_50
        FROM st_play WHERE {w} AND play_kind = 'field_goal' GROUP BY 1 ORDER BY 1 DESC
    """, "made::DOUBLE / nullif(att,0) AS rate"),

    "punt": ("""
        SELECT season,
               count(*)                                          AS punts,
               avg(punt_gross_yds)                               AS gross,
               avg(punt_net_yds)                                 AS net,
               max(punt_gross_yds)                               AS longest,
               avg(return_yds) FILTER (outcome = 'Returned')      AS ret_allowed,
               count(*) FILTER (outcome = 'Fair catch')           AS fair_catch,
               count(*) FILTER (outcome = 'Downed')               AS downed,
               count(*) FILTER (outcome = 'Out of bounds')        AS oob,
               count(*) FILTER (outcome = 'Touchback')            AS touchback,
               count(*) FILTER (outcome = 'Returned')             AS returned,
               count(*) FILTER (outcome = 'Blocked')              AS blocked,
               count(*) FILTER (outcome = 'Unknown')              AS unknown
        FROM st_play WHERE {w} AND play_kind = 'punt' GROUP BY 1 ORDER BY 1 DESC
    """, "unknown::DOUBLE / nullif(punts,0) AS unk_share"),

    "kickoff": ("""
        SELECT season,
               count(*)                                          AS kickoffs,
               avg(kickoff_yds)                                   AS avg_dist,
               count(*) FILTER (NOT COALESCE(onside, FALSE))      AS denom,
               count(*) FILTER (outcome = 'Touchback')            AS touchback,
               count(*) FILTER (outcome = 'Returned')             AS returned,
               count(*) FILTER (outcome = 'Fair catch')           AS fair_catch,
               count(*) FILTER (outcome = 'Out of bounds')        AS oob,
               count(*) FILTER (COALESCE(onside, FALSE))          AS onside,
               avg(return_yds) FILTER (outcome = 'Returned')      AS ret_allowed,
               count(*) FILTER (returned_for_td)                  AS ret_td,
               count(*) FILTER (outcome = 'Unknown')              AS unknown
        FROM st_play WHERE {w} AND play_kind = 'kickoff' GROUP BY 1 ORDER BY 1 DESC
    """, "touchback::DOUBLE / nullif(denom,0) AS tb_rate, "
         "returned::DOUBLE / nullif(denom,0) AS ret_rate, "
         "unknown::DOUBLE / nullif(kickoffs,0) AS unk_share"),
}


# The kick kinds that have a season table above. The conversion chip added to the
# kicks lens has no such table -- a PAT has no distance, no return and no outcome mix
# worth a per-season grid -- so the profile pages iterate this rather than the chip
# list, and a tab can never open onto a missing query.
SEASON_KINDS = ["field_goal", "punt", "kickoff"]


def season_rows(kind: str, where: str) -> list[dict]:
    body, derived = _SEASON_SQL[kind]
    df = data.q(f"SELECT *, {derived} FROM ({body.format(w=where)}) t ORDER BY season DESC")
    return data.records(df)


def season_cols(kind: str) -> list[dict]:
    season = num("season", "Season", 86)
    if kind == "field_goal":
        return [season, num("att", "Att", 72, COM), num("made", "Made", 78, COM),
                num("rate", "Rate", 82, PCT), num("longest", "Long", 78),
                num("avg_dist", "Avg dist", 96, ONE), num("blocked", "Blkd", 74, COM),
                num("m_u30", "<30 made", 96, COM), num("a_u30", "<30 att", 92, COM),
                num("m_30", "30s made", 98, COM), num("a_30", "30s att", 92, COM),
                num("m_40", "40s made", 98, COM), num("a_40", "40s att", 92, COM),
                num("m_50", "50+ made", 98, COM), num("a_50", "50+ att", 92, COM)]
    if kind == "punt":
        return [season, num("punts", "Punts", 82, COM), num("gross", "Gross", 84, ONE),
                num("net", "Net", 78, ONE), num("longest", "Long", 78),
                num("ret_allowed", "Ret allowed", 110, ONE),
                num("fair_catch", "Fair catch", 104, COM),
                num("downed", "Downed", 88, COM), num("oob", "OOB", 74, COM),
                num("touchback", "TB", 68, COM), num("returned", "Returned", 96, COM),
                num("blocked", "Blkd", 74, COM),
                num("unknown", "Unknown", 96, COM),
                num("unk_share", "Unk %", 84, PCT)]
    return [season, num("kickoffs", "KOs", 78, COM), num("avg_dist", "Avg dist", 96, ONE),
            num("tb_rate", "TB rate", 90, PCT), num("ret_rate", "Ret rate", 94, PCT),
            num("ret_allowed", "Ret allowed", 110, ONE),
            num("touchback", "TB", 68, COM), num("returned", "Returned", 96, COM),
            num("fair_catch", "Fair catch", 104, COM), num("oob", "OOB", 74, COM),
            num("onside", "Onside", 86, COM), num("ret_td", "Ret TD", 84, COM),
            num("unknown", "Unknown", 96, COM), num("unk_share", "Unk %", 84, PCT)]


# --------------------------------------------------------------------------- entity plays
def entity_where(ent: dict, flt: dict | None) -> str:
    """The entity's own kicks, under the sidebar filters minus the facet it *is*.

    The lens is forced to the kicks: these pages profile placekickers, punters and
    kickoff specialists, so reading them through the offense lens would apply
    scrimmage-only facets to a view that has no such columns.
    """
    ignore = ("players",) if ent.get("kind") == "player" else ("teams",)
    base = data.where_from_filters(dict(flt or {}, lens="st"), ignore=ignore)
    col = "player_id" if ent.get("kind") == "player" else "team_id"
    return f"({base}) AND {col} = {int(ent['id'])}"


def st_chips(flt: dict | None) -> list[str]:
    """The kick phases the sidebar has selected, whatever lens it is showing.

    The profile pages profile placekickers, punters and kickoff specialists, so they
    read the kicks whatever the header is set to. Chips belonging to another lens are
    dropped by data.chip_set, which falls back to that lens's defaults.
    """
    return data.chip_set(dict(flt or {}, lens="st"))


def play_grid(mode: str, phases, extra=None, height: str = "480px"):
    return html.Div([
        ui.note("Click any row to open the play.", "neutral"),
        ui.grid("grid-ent", mode, infinite=True, height=height,
                columns=columns.build("st", phases, extra, mode)),
    ])


@callback(
    Output("grid-ent", "getRowsResponse"),
    Input("grid-ent", "getRowsRequest"),
    State("ent", "data"), State("flt", "data"),
    prevent_initial_call=True,
)
def _ent_rows(req, ent, flt):
    if not req or not ent:
        raise PreventUpdate
    where = (f"{entity_where(ent, flt)} AND "
             f"({data.filter_model_to_sql(req.get('filterModel'))})")
    order = data.sort_model_to_sql(req.get("sortModel"))
    start = int(req.get("startRow") or 0)
    end = int(req.get("endRow") or (start + ui.BLOCK))
    df = data.q(f"""
        SELECT * FROM st_play WHERE {where}
        ORDER BY {order} LIMIT {max(end - start, 1)} OFFSET {start}
    """)
    return {"rowData": data.records(df),
            "rowCount": data.count_rows("st_play", where)}


@callback(
    Output("detail-uid", "data", allow_duplicate=True),
    Input("grid-ent", "selectedRows"),
    prevent_initial_call=True,
)
def _ent_open(rows):
    if not rows:
        raise PreventUpdate
    return rows[0].get("play_uid")


# --------------------------------------------------------------------------- chrome
def header_block(title: str, badges: list, meta_lines: list, actions=None):
    return dmc.Stack(gap=6, children=[
        dmc.Group(gap="sm", align="center", children=[
            dmc.Text(title, fw=650, size="xl"),
            *badges,
        ]),
        dmc.Group(gap="lg", children=[
            dmc.Text(line, size="xs", c="dimmed") for line in meta_lines if line
        ]),
        actions or html.Div(),
    ])


def phase_tabs(present: list[str]) -> list[dict]:
    """Kick phases only: the profile pages are kicking-side for now."""
    return [{"value": k, "label": lens.PHASES["st"][k]}
            for k in lens.PHASE_ORDER["st"] if k in present]
