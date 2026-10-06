"""Read-only MCP access to the play-by-play warehouse: college football and the NFL.

Twenty-five tools over a SELECT-only Postgres role -- twenty-two typed, plus
schema introspection and a guarded SQL passthrough.

WHAT A CALLER NEEDS TO KNOW, AND WHY THE TOOLS ARE SHAPED THIS WAY
------------------------------------------------------------------
This corpus has three traps that return a plausible wrong number rather than an
error. Every tool below is built so that a caller who knows nothing about them
still gets the right answer:

1. **The conference trap.** Conference membership is grained (team, season) and
   83 of 275 teams changed conference inside the window. Every tool reads the
   wide tables, where that join is resolved at build time.

2. **NULL is meaningful, in three different ways.** `fg_made IS NULL` is a kick
   wiped out by penalty -- not a miss. `returned IS NULL` is an outcome the feed
   never stated (9.9% of punts and kickoffs) on those kinds, and "does not apply"
   on field goals. Nothing here coalesces a NULL flag to false; rate tools state
   their denominator and return the unstated share beside the rate.

3. **Names are not identity.** 109 names are shared by more than one athlete, and
   that is correct -- they are different people. Everything player-grain groups
   by `athlete_id`.

Call `known_limits()` before quoting any number from a season still in progress,
or any rate near a rule change.

SECURITY
--------
Read-only is enforced in four independent layers, of which this file is none:
the role holds SELECT and nothing else; every transaction is read-only; the
statement timeout is set at the role and again per connection; and guard.py
parses model-generated SQL before it reaches the planner. See README.md section 6.
"""
from __future__ import annotations

import datetime as dt
import decimal
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import config, db, guard, queries as q

mcp = MCPServer(
    name=f"pbp-{config.SCHEMA}",
    instructions=(
        f"Play-by-play warehouse for {config.LABEL.upper()} ONLY -- every play from "
        "2014 to last weekend. Two facts: KICKS (kickoffs, punts, field goals, "
        "conversions) and SCRIMMAGE (rushes, passes, sacks, penalties); they are "
        "disjoint and share one play_uid space.\n\n"
        "THIS SERVER SERVES ONE LEAGUE. The other corpus lives behind a separate "
        "server and is not readable from here, so no tool takes a `league` argument "
        "and no query needs one. A question about the other league belongs to the "
        "other server; do not try to answer it from this data.\n\n"
        "Start with data_coverage to see what seasons exist and which are still "
        "being played, and find_team / find_player to turn a name into an id -- "
        "always query by id, because some athlete names are shared by more than one "
        "person and they are different people.\n\n"
        "For anything the typed tools do not cover, use run_sql: call list_schema "
        "once, describe_table for columns and their caveats, then one read-only "
        "SELECT. Table names are unqualified or prefixed with this corpus's schema; "
        f"prefer {config.SCHEMA}.play_wide and {config.SCHEMA}.scrimmage_wide -- "
        "they carry the dimensions pre-joined, including the (team_id, season) "
        "conference resolution that a hand-written join gets wrong.\n\n"
        "PLAYER CAREERS HERE ARE LEAGUE-SCOPED. first_season, last_season and the "
        "play counts in dim_athlete count this league's plays only, so a player who "
        "also played in the other league has a separate, unlinked row there. Never "
        "present these numbers as a whole career without saying which league.\n\n"
        "NULL is meaningful here and must not be coalesced away: fg_made IS NULL "
        "is a kick negated by penalty, not a miss. Call known_limits() before "
        "quoting a number."
    ),
)

DEFAULT_ROWS = 50
MAX_ROWS = 500


# --------------------------------------------------------------------------- helpers

def _clamp(limit: int | None, default: int = DEFAULT_ROWS) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_ROWS))


def _jsonable(rows: list[dict]) -> list[dict]:
    """Make psycopg's Python types serialisable without losing precision.

    Decimal goes to float only when it is not integral, so a count stays an int
    and a percentage stays a float. Dates and timestamps go to ISO strings.
    """
    out: list[dict] = []
    for row in rows:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            clean[key] = _scalar(value)
        out.append(clean)
    return out


def _scalar(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.days
    return value


def _jsonable_rows(rows: list[list]) -> list[list]:
    return [[_scalar(v) for v in row] for row in rows]


def _one_of(value: str | None, allowed: tuple[str, ...], field: str) -> str | None:
    """Validate a caller string against an allow-list.

    Returns the ALLOW-LIST's own string, never the caller's, so anything that
    reaches SQL as an identifier is a module constant by construction.
    """
    if value is None:
        return None
    lowered = value.strip().lower()
    for candidate in allowed:
        if candidate.lower() == lowered:
            return candidate
    raise ValueError(
        f"{field} must be one of {', '.join(allowed)} -- got {value!r}."
    )


# --------------------------------------------------------------------------- coverage

@mcp.tool()
def data_coverage() -> dict:
    """What is in the warehouse: seasons, games, and which season is still live.

    Call this first for any question about a recent season. A season still being
    played must be held out of anything fitted or compared season-over-season --
    `is_in_progress` is how you tell, and it is self-maintaining rather than a
    hardcoded year.

    The `kicks` column counts special-teams plays only (it is built off the kicks
    fact); `scrimmage_plays` in the totals block is the other fact.
    """
    seasons = db.query(q.DATA_COVERAGE)
    totals = db.query(q.CORPUS_TOTALS)
    live = [r for r in seasons if r.get("is_in_progress")]
    return {
        # One row now, not one per league: this server serves one corpus.
        "totals": _jsonable(totals)[0] if totals else {},
        "league": config.LEAGUE,
        "seasons": _jsonable(seasons),
        "in_progress": _jsonable(live),
        "note": (
            "A season with is_in_progress=true is incomplete and its rates are not "
            "trend points. 2020 is two-thirds of a college season by reality, not by "
            "ingestion, and 2014 PAT/two-point rates are not comparable to later years "
            "-- see known_limits()."
        ),
    }


# --------------------------------------------------------------------------- discovery

@mcp.tool()
def find_team(
    query: str | None = None,
    season: int | None = None,
    limit: int | None = None,
) -> dict:
    """Turn a team name into a (league, team_id) pair, with its conference.

    ALWAYS carry `league` forward with the id. NFL team ids run 1-34 and collide
    outright with college ids -- team 2 is Auburn and also the Buffalo Bills -- so
    a team id on its own is ambiguous, and a league-blind join matches two
    dimension rows per play and doubles the table.

    `season` controls which season's conference is reported; without it you get
    the team's most recent one, which is the WRONG label for a historical
    question. Ask for the season you are analysing.
    """
    rows = db.query(q.FIND_TEAM, {
        "q": query,
        "season": season,
        "limit": _clamp(limit),
    })
    return {
        "count": len(rows),
        "teams": _jsonable(rows),
        "note": "team_id is only meaningful together with league; they collide between leagues.",
    }


@mcp.tool()
def find_player(
    name: str | None = None,
    position: str | None = None,
    season: int | None = None,
    limit: int | None = None,
) -> dict:
    """Turn a player name into an athlete_id. Query by id from then on.

    109 names in this corpus are shared by more than one athlete and that is
    correct -- they are different people. If this returns several rows for one
    name, they are several players; disambiguate on position, team and seasons
    rather than picking the first.

    `leagues` says where the career happened: `cfb`, `nfl`, or `cfb+nfl`. 2,779
    people have one row spanning both corpora, which is the cross-league question
    this schema exists to answer -- the same athlete_id works in both.

    Results are ordered by total plays, so the best-known holder of a name comes
    first. That is a convenience, not a judgement about which one you meant.
    """
    rows = db.query(q.FIND_PLAYER, {
        "name": name,
        "position": position,
        "season": season,
        "limit": _clamp(limit),
    })
    return {
        "count": len(rows),
        "players": _jsonable(rows),
        "note": (
            "Group by athlete_id, label with known_name -- never the reverse. "
            "first_season/last_season and the play counts are CAREER aggregates "
            "across both facts and both leagues."
        ),
    }


@mcp.tool()
def find_venue(
    query: str | None = None,
    state: str | None = None,
    indoor: bool | None = None,
    limit: int | None = None,
) -> dict:
    """Find a stadium by name or city. Venue ids are SHARED across both leagues.

    Unlike team ids, venue ids are one id space -- a bowl game at AT&T Stadium is
    the same building as a Cowboys home game, under the same id.

    `indoor` is a STADIUM property, not a game condition. A retractable roof reads
    true whether or not it was open that day, and 5 of the 18 indoor venues are
    retractable (AT&T, State Farm, Mercedes-Benz, Allegiant, Lucas Oil). So
    `indoor=false` is a clean "weather applied"; `indoor=true` means "weather may
    not have applied, and the feed cannot say which". There is no lat/lon.
    """
    rows = db.query(q.FIND_VENUE, {
        "q": query, "state": state, "indoor": indoor, "limit": _clamp(limit),
    })
    return {"count": len(rows), "venues": _jsonable(rows)}


@mcp.tool()
def list_conferences(season: int | None = None) -> dict:
    """Conferences, with member counts for a season.

    Conference ids collide between leagues exactly as team ids do -- 8 is the SEC
    and also the AFC -- so `league` is part of the key.
    """
    rows = db.query(q.LIST_CONFERENCES, {
        "season": season,
    })
    return {"count": len(rows), "conferences": _jsonable(rows)}


# --------------------------------------------------------------------------- games

@mcp.tool()
def list_games(
    season: int | None = None,
    week: int | None = None,
    season_type: str | None = None,
    team_id: int | None = None,
    limit: int | None = None,
) -> dict:
    """List games, newest first. `team_id` matches either home or away.

    `team_id` needs `league` alongside it to be unambiguous.
    """
    rows = db.query(q.LIST_GAMES, {
        "season": season,
        "week": week,
        "season_type": _one_of(season_type, q.SEASON_TYPES, "season_type"),
        "team_id": team_id,
        "limit": _clamp(limit),
    })
    return {"count": len(rows), "games": _jsonable(rows)}


@mcp.tool()
def game_summary(game_id: int) -> dict:
    """One game: teams, venue, play counts, and a DERIVED final score.

    **The score is derived, not stored.** This is a play-level corpus with no
    scoreboard in it, so the score here is summed from scoring plays the same way
    `reports/margins.py` does. It is usually right and is not authoritative:
    ESPN's own score column lags a play and was repaired on 2026-09-08 with
    residue, and 38 conversions on return touchdowns sit on the wrong team. Treat
    a one-or-two-point discrepancy against a box score as expected.
    """
    header = db.query(q.GAME_HEADER, (game_id,))
    if not header:
        return {"error": f"No game with id {game_id}. Try list_games to find one."}
    score = db.query(q.GAME_SCORE, {"game_id": game_id})
    counts = db.query(q.GAME_PLAY_COUNTS, {"game_id": game_id})
    return {
        "game": _jsonable(header)[0],
        "derived_score": _jsonable(score),
        "play_counts": _jsonable(counts),
        "note": (
            "derived_score is summed from scoring plays, not read from a scoreboard "
            "column -- the warehouse has none. See the tool description."
        ),
    }


@mcp.tool()
def drive_chart(game_id: int) -> dict:
    """Every drive in one game, in order.

    A drive SPANS BOTH FACTS -- a drive that ends in a punt contains the punt -- so
    `plays_total` counts every play and `plays_scrimmage` only those that reached
    the scrimmage fact. `start_yards_to_goal` is measured from the possessing
    team's own goal (taken from the first play, not from ESPN's own drive column,
    which is measured in a fixed direction and reads backwards for one team).
    """
    rows = db.query(q.DRIVE_CHART, (game_id,))
    if not rows:
        return {"error": f"No drives for game {game_id}."}
    return {"count": len(rows), "drives": _jsonable(rows)}


# --------------------------------------------------------------------------- plays

# Filter fragments for query_plays. The KEY is what a caller may ask for; the
# VALUE is a fixed SQL fragment with a bound placeholder. A caller never supplies
# SQL, only a key and a value -- the fragment is a module constant.
# Every key here IS a parameter of query_plays. Side-specific id filters
# (kicking_team_id, kicker_athlete_id, ...) are deliberately absent: `team_id` and
# `athlete_id` match either side, and one-sided filtering goes through run_sql. An
# unreachable key in an allow-list reads like a supported filter and is not one.
_KICK_FILTERS = {
    "season":       "season = %(season)s",
    "week":         "week = %(week)s",
    "season_type":  "season_type = %(season_type)s",
    "play_kind":    "play_kind = %(play_kind)s",
    "game_id":      "game_id = %(game_id)s",
    "venue_id":     "venue_id = %(venue_id)s",
    "min_fg_distance": "fg_distance_yds >= %(min_fg_distance)s",
    "max_fg_distance": "fg_distance_yds <= %(max_fg_distance)s",
    "fg_made":      "fg_made = %(fg_made)s",
    "touchback":    "touchback = %(touchback)s",
    "returned":     "returned = %(returned)s",
    "is_clutch":    "is_clutch = %(is_clutch)s",
    "top_division_only": "both_top_division",
    "text_contains": "play_text ILIKE '%%' || %(text_contains)s || '%%'",
}

_SCRIMMAGE_FILTERS = {
    "season":       "season = %(season)s",
    "week":         "week = %(week)s",
    "season_type":  "season_type = %(season_type)s",
    "play_kind":    "play_kind = %(play_kind)s",
    "game_id":      "game_id = %(game_id)s",
    "venue_id":     "venue_id = %(venue_id)s",
    "down":         "down = %(down)s",
    "min_yards":    "yards_gained >= %(min_yards)s",
    "max_yards":    "yards_gained <= %(max_yards)s",
    "is_touchdown": "is_touchdown = %(is_touchdown)s",
    "is_turnover":  "is_turnover = %(is_turnover)s",
    "first_down_gained": "first_down_gained = %(first_down_gained)s",
    "is_clutch":    "is_clutch = %(is_clutch)s",
    "top_division_only": "both_top_division",
    "text_contains": "play_text ILIKE '%%' || %(text_contains)s || '%%'",
}


@mcp.tool()
def query_plays(
    fact: str,
    season: int | None = None,
    week: int | None = None,
    season_type: str | None = None,
    play_kind: str | None = None,
    game_id: int | None = None,
    team_id: int | None = None,
    athlete_id: int | None = None,
    venue_id: int | None = None,
    down: int | None = None,
    min_yards: int | None = None,
    max_yards: int | None = None,
    min_fg_distance: int | None = None,
    max_fg_distance: int | None = None,
    fg_made: bool | None = None,
    touchback: bool | None = None,
    returned: bool | None = None,
    first_down_gained: bool | None = None,
    is_touchdown: bool | None = None,
    is_turnover: bool | None = None,
    is_clutch: bool | None = None,
    top_division_only: bool = False,
    text_contains: str | None = None,
    limit: int | None = None,
) -> dict:
    """Filtered play rows from either fact. `fact` is "kicks" or "scrimmage".

    The workhorse for "show me the plays where ...". Every row carries `play_text`
    verbatim from ESPN, so any answer can be audited back to what the feed said.

    `team_id` and `athlete_id` match EITHER side: on kicks, `team_id` matches the
    kicking or receiving team and `athlete_id` the kicker or returner; on
    scrimmage, `team_id` matches offense or defense and `athlete_id` any of the
    four denormalised roles. Use the specific filters through run_sql if you need
    one side only.

    `returned` (kicks) is tri-state and NULL is not reachable through this filter:
    passing false means "the feed said it was not returned", not "the feed did not
    say". For the unstated rows use run_sql with `returned IS NULL`, and read
    known_limits() first -- there are 20,950 of them.

    `is_clutch` is 4th quarter or later, one score either way, under five minutes.
    `top_division_only` restricts college to FBS-vs-FBS and is constant-true for
    the NFL -- set it for any kicker-quality question.

    Rows are capped; this returns plays, not aggregates. For a rate, use one of
    the analysis tools or run_sql.
    """
    table_key = _one_of(fact, tuple(q.FACTS), "fact")
    table = q.FACTS[table_key]
    kinds = q.KICK_KINDS if table_key == "kicks" else q.SCRIMMAGE_KINDS
    allowed = _KICK_FILTERS if table_key == "kicks" else _SCRIMMAGE_FILTERS

    params: dict[str, Any] = {
        "season": season,
        "week": week,
        "season_type": _one_of(season_type, q.SEASON_TYPES, "season_type"),
        "play_kind": _one_of(play_kind, kinds, "play_kind"),
        "game_id": game_id,
        "venue_id": venue_id,
        "down": down,
        "min_yards": min_yards,
        "max_yards": max_yards,
        "min_fg_distance": min_fg_distance,
        "max_fg_distance": max_fg_distance,
        "fg_made": fg_made,
        "touchback": touchback,
        "returned": returned,
        "first_down_gained": first_down_gained,
        "is_touchdown": is_touchdown,
        "is_turnover": is_turnover,
        "is_clutch": is_clutch,
        "text_contains": text_contains,
    }

    clauses: list[str] = []
    for key, fragment in allowed.items():
        if key in ("top_division_only",):
            continue
        if params.get(key) is not None:
            clauses.append(fragment)

    if top_division_only:
        clauses.append(allowed["top_division_only"])

    # Either-side matching. Both branches are fixed fragments; only the value is bound.
    if team_id is not None:
        params["team_id"] = team_id
        clauses.append(
            "(kicking_team_id = %(team_id)s OR receiving_team_id = %(team_id)s)"
            if table_key == "kicks" else
            "(offense_team_id = %(team_id)s OR defense_team_id = %(team_id)s)"
        )
    if athlete_id is not None:
        params["athlete_id"] = athlete_id
        clauses.append(
            "(kicker_athlete_id = %(athlete_id)s OR returner_athlete_id = %(athlete_id)s"
            " OR tackler_athlete_id = %(athlete_id)s)"
            if table_key == "kicks" else
            "(passer_athlete_id = %(athlete_id)s OR rusher_athlete_id = %(athlete_id)s"
            " OR receiver_athlete_id = %(athlete_id)s OR tackler_athlete_id = %(athlete_id)s)"
        )

    params["limit"] = _clamp(limit)
    where = " AND ".join(clauses) if clauses else "TRUE"

    # `table` and `where` are built entirely from module constants above; every
    # caller value is in `params` and bound by the driver.
    sql = (
        f"SELECT * FROM {table} WHERE {where} "
        "ORDER BY season DESC, game_id, play_uid LIMIT %(limit)s"
    )
    rows = db.query(sql, params)
    return {
        "fact": table_key,
        "table": table,
        "count": len(rows),
        "truncated": len(rows) >= params["limit"],
        "plays": _jsonable(rows),
    }


@mcp.tool()
def play_detail(play_uid: str) -> dict:
    """One play in full, with every athlete ESPN reported on it.

    `play_uid` is unique across BOTH facts by construction, so this finds the play
    wherever it lives. The participant list is the full truth -- the id columns on
    the fact row are a denormalised hot path holding only the first athlete per
    role, and 193,215 scrimmage plays have two tacklers.
    """
    row = db.query(q.PLAY_DETAIL_KICKS, (play_uid,))
    fact = "kicks"
    if not row:
        row = db.query(q.PLAY_DETAIL_SCRIMMAGE, (play_uid,))
        fact = "scrimmage"
    if not row:
        return {"error": f"No play with uid {play_uid!r}."}
    people = db.query(q.PLAY_PARTICIPANTS, {"play_uid": play_uid})
    return {
        "fact": fact,
        "play": _jsonable(row)[0],
        "participants": _jsonable(people),
    }


# --------------------------------------------------------------------------- players

@mcp.tool()
def player_profile(athlete_id: int, season: int | None = None) -> dict:
    """One athlete: identity plus career production, split by league and season.

    Split rather than blended on purpose. 2,779 people played in both leagues, and
    a single career line across them would average a college season against an NFL
    one -- two different sports at the margin.

    Identity fields are CAREER aggregates taken across every season at once:
    `first_season`, `last_season`, `primary_team_id` and the modal `known_name`.
    `primary_role` describes the player, not the row that linked them.

    Rushing and passing means exclude turnovers, because ESPN's yardage on a
    turnover is the DEFENCE's return, not the offence's gain.
    """
    profile = db.query(q.PLAYER_PROFILE, (athlete_id,))
    if not profile:
        return {"error": f"No athlete with id {athlete_id}. Try find_player."}
    kicking = db.query(q.PLAYER_KICKING, {"athlete_id": athlete_id, "season": season})
    scrimmage = db.query(q.PLAYER_SCRIMMAGE, {"athlete_id": athlete_id, "season": season})
    return {
        "player": _jsonable(profile)[0],
        "kicking_by_season": _jsonable(kicking),
        "scrimmage_by_season": _jsonable(scrimmage),
        "note": (
            "fg_att excludes kicks negated by penalty (fg_made IS NULL) rather than "
            "counting them as misses. Yardage means exclude turnovers."
        ),
    }


@mcp.tool()
def player_game_log(
    athlete_id: int, season: int | None = None, limit: int | None = None
) -> dict:
    """Per-game scrimmage production for one athlete, newest first.

    Scrimmage only -- for a kicker's game-by-game work use query_plays with
    fact="kicks" and the athlete id.
    """
    rows = db.query(q.PLAYER_GAME_LOG, {
        "athlete_id": athlete_id, "season": season, "limit": _clamp(limit),
    })
    return {"count": len(rows), "games": _jsonable(rows)}


@mcp.tool()
def leaderboard(
    measure: str,
    season: int | None = None,
    min_attempts: int = 20,
    top_division_only: bool = False,
    limit: int | None = None,
) -> dict:
    """Rank players on one measure. Always qualified by a minimum attempt count.

    measure: fg_pct | punt_gross | rush_yards | pass_yards | receiving_yards | tackles

    `min_attempts` defaults to 20 and exists because an unqualified rate
    leaderboard is the most reliable way to get a confidently wrong answer here --
    a 100% field-goal kicker with two attempts leads nothing. Raise it for rate
    measures over a full career; lower it only deliberately.

    `top_division_only` restricts college to FBS-vs-FBS games and is constant-true
    for the NFL. Set it for any question about kicker or player quality, since the
    corpus deliberately includes FBS-vs-FCS games.

    Without `season` this ranks whole careers within the window; with it, one
    season. Results are grouped by league, so a cross-league career appears once
    per league rather than blended.
    """
    key = _one_of(measure, tuple(q.LEADERBOARD_SQL), "measure")
    rows = db.query(q.LEADERBOARD_SQL[key], {
        "season": season,
        "min_attempts": max(1, int(min_attempts)),
        "top_division_only": bool(top_division_only),
        "limit": _clamp(limit),
    })
    return {
        "measure": key,
        "min_attempts": max(1, int(min_attempts)),
        "count": len(rows),
        "leaders": _jsonable(rows),
    }


# --------------------------------------------------------------------------- teams

@mcp.tool()
def team_season(team_id: int, season: int) -> dict:
    """One team's offence, defence and special teams for one season.

    `league` is required, not optional: team ids collide between the two corpora.

    Offence and defence are read from the two sides of the same scrimmage fact,
    so `off_plays` for one team and `def_plays` for its opponents count the same
    events from opposite ends. Nothing is duplicated to achieve that.
    """
    rows = db.query(q.TEAM_SEASON, {
        "team_id": team_id, "season": season,
    })
    if not rows or rows[0].get("team") is None:
        return {
            "error": (
                f"No team {team_id} in this corpus. Check find_team."
            )
        }
    return {"team_season": _jsonable(rows)[0]}


# --------------------------------------------------------------------------- analysis

@mcp.tool()
def fg_by_distance(
    season: int | None = None,
    top_division_only: bool = False,
) -> dict:
    """Field goal percentage by 5-yard distance band.

    Monotonic in both leagues over 30k college and 12.8k NFL attempts. Kicks
    negated by penalty are excluded from the denominator rather than counted as
    misses; a distance the parser could not recover is excluded entirely.
    """
    rows = db.query(q.FG_BY_DISTANCE, {
        "season": season,
        "top_division_only": bool(top_division_only),
    })
    return {"count": len(rows), "buckets": _jsonable(rows)}


@mcp.tool()
def kick_outcomes(
    play_kind: str = "kickoff",
    season: int | None = None,
    top_division_only: bool = False,
) -> dict:
    """Touchback / return / fair catch rates by season, on the right denominator.

    play_kind: kickoff | punt (the only two kinds where these outcomes apply).

    **Read `pct_unstated` before quoting any rate here.** 9.9% of punts and
    kickoffs state no outcome in the feed at all; those rows are excluded from
    the rates rather than counted as "did not happen", and `pct_unstated` tells
    you how much of the season is actually quotable. Onside kicks are excluded --
    a different play, not an outcome.

    This is the measure that shows both leagues' rule changes: college steps in
    2018 (the fair-catch rule) and the NFL collapses in two stages, 2024 (the
    dynamic kickoff) and 2025 (the touchback spot moving to the 35).
    """
    kind = _one_of(play_kind, ("kickoff", "punt"), "play_kind")
    rows = db.query(q.KICK_OUTCOMES, {
        "kinds": [kind],
        "season": season,
        "top_division_only": bool(top_division_only),
    })
    return {
        "play_kind": kind,
        "count": len(rows),
        "seasons": _jsonable(rows),
        "note": (
            "Rates are over rows whose outcome the feed stated (`classified`). "
            "`pct_unstated` is the share excluded -- it is not zero and it varies "
            "by season."
        ),
    }


@mcp.tool()
def drive_outcomes(season: int | None = None) -> dict:
    """Drive result by starting field position.

    Monotonic in both leagues -- 51.9% touchdowns starting inside the opponent's
    20 down to 20.1% from an own-10 start in college, 53.3% to 19.9% in the NFL.
    `start_yards_to_goal = 0` is a null sentinel, not the goal line, and is
    excluded.
    """
    rows = db.query(q.DRIVE_OUTCOMES, {
        "season": season,
    })
    return {"count": len(rows), "zones": _jsonable(rows)}


@mcp.tool()
def situational_splits(
    season: int | None = None,
    top_division_only: bool = False,
) -> dict:
    """Scrimmage efficiency by down, distance bucket and field zone.

    distance_bucket: short (<=3) | medium (4-7) | long (8+).
    field_zone: red zone (<=20 to goal) | opponent half | own half.

    Yardage means exclude turnovers -- ESPN's yardage on a turnover is the
    defence's return, not the offence's gain, so including them credits a 35-yard
    pick-six to the offence.
    """
    rows = db.query(q.SITUATIONAL_SPLITS, {
        "season": season,
        "top_division_only": bool(top_division_only),
    })
    return {"count": len(rows), "splits": _jsonable(rows)}


@mcp.tool()
def league_trend(
    measure: str, top_division_only: bool = False
) -> dict:
    """One measure by season, for trend and rule-change questions.

    measure: fg_pct | kickoff_touchback_pct | punt_gross_avg | pat_pct |
             completion_pct | yards_per_carry | yards_per_pass_attempt |
             drive_score_pct

    `n` is the denominator for each season, and it matters: the most recent
    season is usually still being played, so its point is not comparable. Check
    data_coverage.
    """
    key = _one_of(measure, tuple(q.TREND_SQL), "measure")
    rows = db.query(q.TREND_SQL[key], {
        "top_division_only": bool(top_division_only),
    })
    return {
        "measure": key,
        "count": len(rows),
        "series": _jsonable(rows),
        "note": "The latest season may be in progress -- call data_coverage.",
    }


# --------------------------------------------------------------------------- quality

@mcp.tool()
def parse_quality(season: int | None = None) -> dict:
    """Parser agreement and athlete-id coverage on the kicks fact, by season.

    The kicks fact is the only parsed half of the warehouse -- ~98% `exact` in
    both corpora, from two separate dialect modules. The
    scrimmage fact needs no parser at all -- its fields are structured -- so it
    has no equivalent and is absent here.

    A season whose pct_exact drops sharply is a parser regression, not a data
    quirk. Use audit_plays to see the rows.
    """
    rows = db.query(q.PARSE_QUALITY, {
        "season": season,
    })
    return {"count": len(rows), "quality": _jsonable(rows)}


@mcp.tool()
def audit_plays(
    season: int | None = None,
    play_kind: str | None = None,
    limit: int | None = None,
) -> dict:
    """Kicks the parser could not read exactly, with their raw ESPN text.

    This is the audit trail. `play_text` is never cleaned or repaired anywhere in
    this warehouse precisely so that any row can be checked against what the feed
    actually said.
    """
    rows = db.query(q.AUDIT_PLAYS, {
        "season": season,
        "play_kind": _one_of(play_kind, q.KICK_KINDS, "play_kind"),
        "limit": _clamp(limit),
    })
    return {"count": len(rows), "plays": _jsonable(rows)}


# The caveats that change an answer, phrased for a caller who is about to quote a
# number. This mirrors README.md "Known limits", which is the source of truth --
# if the two disagree, the README is right and this needs updating.
_KNOWN_LIMITS = [
    {
        "topic": "unstated outcomes",
        "applies_to": "kicks fact, punts and kickoffs",
        "limit": "Some kicks state no outcome at all; `returned IS NULL` marks them. "
                  "THE RATE DIFFERS SHARPLY BY CORPUS and the often-quoted 9.9% is the "
                  "COLLEGE figure, not a corpus-wide one: 21,346 of 216,657 college "
                  "punts and kickoffs (9.9%) against 2,097 of 62,313 in the NFL (3.4%). "
                  "The blended 8.4% describes neither.",
        "what_to_do": "Exclude them from rate denominators (kick_outcomes already "
                      "does) and report pct_unstated beside any rate.",
    },
    {
        "topic": "NULL means two things",
        "applies_to": "kicks fact",
        "limit": "`returned IS NULL` is an unstated outcome on punts and kickoffs, "
                 "but means 'does not apply' on field goals and conversions.",
        "what_to_do": "Scope every returned/touchback question to play_kind IN "
                      "('punt','kickoff').",
    },
    {
        "topic": "negated kicks",
        "applies_to": "kicks fact",
        "limit": "`fg_made IS NULL` is a kick wiped out by penalty, not a miss.",
        "what_to_do": "Never coalesce it to false. Exclude from the denominator.",
    },
    {
        "topic": "2014 conversions",
        "applies_to": "both leagues, season 2014",
        "limit": "2014 PAT and two-point rates are not trend points.",
        "what_to_do": "Start conversion trends at 2015.",
    },
    {
        "topic": "2020",
        "applies_to": "college, season 2020",
        "limit": "2020 is two-thirds of a season -- a real gap in play, not in "
                 "ingestion.",
        "what_to_do": "Do not read the smaller counts as a data problem; do not "
                      "compare raw totals against a full season.",
    },
    {
        "topic": "season in progress",
        "applies_to": "the current season, both leagues",
        "limit": "A season still being played is incomplete and weighted toward "
                 "early-season games.",
        "what_to_do": "Call data_coverage and hold out any season with "
                      "is_in_progress=true from anything fitted or compared.",
    },
    {
        "topic": "yards_to_goal = 0",
        "applies_to": "both facts",
        "limit": "0 is a null sentinel, not the goal line -- 1,114 rows.",
        "what_to_do": "Exclude it from any field-position analysis.",
    },
    {
        "topic": "statYardage on turnovers",
        "applies_to": "scrimmage fact",
        "limit": "ESPN's yardage on a turnover is the DEFENCE's return, not the "
                 "offence's gain -- 35 yards is credited to the offence row of a "
                 "35-yard pick-six.",
        "what_to_do": "Exclude turnovers from every mean-yards measure. Every tool "
                      "here already does.",
    },
    {
        "topic": "wallclock_utc",
        "applies_to": "both facts",
        "limit": "96.6% populated overall, but only 66.9% in 2017.",
        "what_to_do": "Any per-play time join loses a third of 2017.",
    },
    {
        "topic": "derived score",
        "applies_to": "game_summary",
        "limit": "There is no score column in this warehouse. game_summary derives "
                 "one; ESPN's own score lagged a play (repaired 2026-09-08, with "
                 "residue) and 38 conversions on return touchdowns sit on the wrong "
                 "team.",
        "what_to_do": "Treat a one-or-two-point discrepancy against a box score as "
                      "expected, not as a bug to chase.",
    },
    {
        "topic": "indoor is a stadium property",
        "applies_to": "dim_venue, both facts",
        "limit": "`indoor` reads true for a retractable roof whether or not it was "
                 "open that day. 5 of the 18 indoor venues are retractable.",
        "what_to_do": "indoor=false is a clean 'weather applied'; indoor=true is "
                      "'cannot say'. There is no lat/lon and no weather in this "
                      "warehouse.",
    },
    {
        "topic": "shared names",
        "applies_to": "dim_athlete",
        "limit": "Some names are shared by more than one athlete, and they are "
                 "different people -- 109 across the combined corpus.",
        "what_to_do": "Group by athlete_id, label with known_name. Never the reverse.",
    },
    {
        "topic": "no cross-league careers",
        "applies_to": "dim_athlete",
        "limit": "This dimension is scoped to THIS corpus. first_season, last_season, "
                 "primary_team_id, primary_role and the play counts are computed from "
                 "this league's plays alone. A player who appears in both corpora has "
                 "a row in each and the two are NOT linked -- there is no career here "
                 "that spans college and the NFL.",
        "what_to_do": "Do not present a player's numbers here as a whole career if "
                      "they also played in the other league. Answering that question "
                      "needs the pbp schema, which this server cannot read.",
    },
    {
        "topic": "colliding ids -- NO LONGER REACHABLE HERE",
        "applies_to": "dim_team, dim_conference",
        "limit": "Upstream, team and conference ids mean different things in each "
                 "league: team 2 is Auburn and also the Buffalo Bills, conference 8 "
                 "is the SEC and also the AFC. THIS SERVER SERVES ONE CORPUS, so the "
                 "collision cannot be reached from here -- the other league is a "
                 "separate schema this connection cannot resolve.",
        "what_to_do": "Nothing. Kept listed because the ids themselves are still "
                      "ambiguous OUTSIDE this server: an id carried into the other "
                      "league's server, or into the pbp schema, means something else.",
    },
    {
        "topic": "FBS vs FCS",
        "applies_to": "college",
        "limit": "The corpus deliberately includes FBS-vs-FCS games.",
        "what_to_do": "Set top_division_only for any player- or kicker-quality "
                      "question. It is constant-true for the NFL, so it is safe to "
                      "set on a cross-league query.",
    },
]


@mcp.tool()
def known_limits(topic: str | None = None) -> dict:
    """The caveats that change an answer. Read before quoting a number.

    Each entry says what the limit is and what to do about it. `topic` filters by
    substring across the topic and the text.

    These mirror README.md "Known limits", which is the source of truth.
    """
    items = _KNOWN_LIMITS
    if topic:
        needle = topic.strip().lower()
        items = [
            item for item in _KNOWN_LIMITS
            if needle in item["topic"].lower()
            or needle in item["limit"].lower()
            or needle in item["applies_to"].lower()
        ]
    return {"count": len(items), "limits": items}


# --------------------------------------------------------------------------- schema

@mcp.tool()
def list_schema() -> dict:
    """The readable tables, with row counts and what each one is for.

    Start here for anything the typed tools do not cover, then describe_table for
    the columns, then run_sql.
    """
    rows = db.query(q.LIST_SCHEMA)
    tables = []
    for row in rows:
        item = dict(row)
        item["note"] = q.TABLE_NOTES.get(row["table_name"])
        tables.append(item)
    return {
        "database": db.database(),
        "schema": config.SCHEMA,      # was a hard-coded "pbp" from before the split
        "count": len(tables),
        "tables": _jsonable(tables),
        "sql_rules": guard.describe_policy(),
    }


@mcp.tool()
def describe_table(table: str) -> dict:
    """Columns, types and caveats for one table.

    Call this before writing SQL against a fact. The column COMMENTs carry the
    NULL semantics and the units, which are not guessable from the names -- they
    live in the database (comment_tables.sql) rather than in a prompt that can
    drift.
    """
    name = table.strip().lower()
    if name.startswith("pbp."):
        name = name[4:]
    if name not in q.READABLE_TABLES:
        return {
            "error": (
                f"{table!r} is not a readable table. Available: "
                + ", ".join(q.READABLE_TABLES)
            )
        }
    meta = db.query(q.TABLE_META, (name,))
    columns = db.query(q.DESCRIBE_TABLE, (name,))
    if not meta:
        return {"error": f"{table!r} exists in the allow-list but not in the database."}
    return {
        "table": _jsonable(meta)[0],
        "note": q.TABLE_NOTES.get(name),
        "column_count": len(columns),
        "columns": _jsonable(columns),
    }


def _run_or_explain(sql: str, explain_only: bool) -> tuple[list[str], list[list]]:
    if explain_only:
        return db.query_guarded(f"EXPLAIN {sql}")
    return db.query_guarded(sql)


@mcp.tool()
def run_sql(sql: str, explain_only: bool = False) -> dict:
    """One read-only SELECT against the warehouse. `explain_only` costs it first.

    Prefer `play_wide` and `scrimmage_wide` (unqualified, or prefixed with this
    server's schema -- there is no `pbp.` schema to read): they carry the dimensions
    pre-joined, including the (team_id, season) conference resolution that a
    hand-written join to dim_team_season gets wrong in both directions.

    The statement is parsed before it is sent. One statement, SELECT only, over
    the allow-listed tables, with a LIMIT forced on. The database role is
    independently read-only, so a rejection here is a clearer error rather than
    the thing protecting the data.

    Results are capped -- check `truncated` and `limit_source` before treating a
    full page as a complete answer.
    """
    try:
        rewritten, report = guard.check(sql)
    except guard.SQLNotAllowed as exc:
        return {"error": str(exc), "rejected": True}

    # EXPLAIN is wrapped around the REWRITTEN statement, so what is costed is what
    # would actually run.
    try:
        columns, rows = _run_or_explain(rewritten, explain_only)
    except Exception as exc:  # surfaced to the model, which can retry
        return {
            "error": str(exc),
            "sql": rewritten,
            "hint": (
                "If this timed out on a join across both facts, aggregate each side "
                "in a CTE first and then join the CTEs -- joining 2M scrimmage rows "
                "to 400k kick rows before aggregating is the usual cause."
            ),
        }

    result = {
        "sql": rewritten,
        "columns": columns,
        "rows": _jsonable_rows(rows),
        "row_count": len(rows),
        "tables": report["tables"],
        "row_limit": report["row_limit"],
        "limit_source": report["limit_source"],
        "truncated": (not explain_only) and len(rows) >= report["row_limit"],
    }
    if explain_only:
        result["explain_only"] = True
    return result


# --------------------------------------------------------------------------- entrypoints

def main() -> None:
    """stdio transport -- how Claude Desktop and Claude Code launch this."""
    try:
        mcp.run()
    finally:
        db.close_all()


def main_http() -> None:
    """streamable-HTTP transport, for running this co-located on the EC2 box.

        ./.venv/bin/python -m pbp_mcp.server --http

    NOT YET DEPLOYED. The weather warehouse runs this way as weather-mcp.service
    on 127.0.0.1:8770; nothing equivalent exists for pbp yet, so this entrypoint
    is provided and smoke-tested but has no systemd unit, service user or env
    file behind it. See README.md section 4.

    `transport="streamable-http"`, not `"http"` -- the SDK accepts only
    stdio | sse | streamable-http, and the wrong string fails at startup rather
    than at import, so it survives every check that does not actually bind.

    BINDS TO 127.0.0.1 AND MUST STAY THAT WAY. There is no authentication on this
    endpoint: anything that can reach it can read the whole warehouse. Port 8770
    is taken by weather-mcp, hence 8771.

    `stateless_http=True` matches the weather deployment, whose client reconnects
    rather than resuming a session; sessions would otherwise accumulate with
    nothing reaping them.
    """
    host = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_HTTP_PORT", "8771"))
    try:
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            streamable_http_path="/mcp",
            stateless_http=True,
        )
    finally:
        db.close_all()


if __name__ == "__main__":
    # `--http` rather than a separate module, so both transports share one import
    # path and a systemd unit launches the same file Claude Desktop does.
    if "--http" in sys.argv:
        main_http()
    else:
        main()
