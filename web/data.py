"""DuckDB access layer for the play-by-play instance explorer.

Three views, one column vocabulary. `st_play` reads the kicks fact, `off_play` and
`def_play` read the scrimmage fact from the two sides of the same 1,510,679 events.
Every one of them exposes `team_id`, `opp_id`, `player_id`, `outcome`, `score_diff`
and the situation columns under the same names, which is what lets the filter
translation, the sort translation and the infinite row model below stay lens-blind:
they interpolate a view name and never learn what is in it.

**Subject-relative is the rule.** On `off_play` the subject is the offense; on
`def_play` it is the defense. `team_id` is whoever the lens is about, `opp_id` is the
other side, and everything signed flips with the subject -- `score_diff` is the
subject's margin and `points_scored` is points the subject gained, so a pick-six is
+7 on defense and -7 on offense. Nothing is duplicated to achieve that: one row in
`snap.scrimmage` is one row in each view, read from opposite ends.

The snapshot is opened READ_ONLY and attached to an in-memory database, so these views
are created without writing to the file and any other reader of the snapshot can keep
using it at the same time.

Two data properties are handled here rather than left to every caller:

  * `statYardage` on a turnover is the *defense's return*, not the offense's gain --
    ESPN credits 35 yards to the offense row of a 35-yard pick-six. Turnovers are
    therefore held out of every mean-yards measure, and the KPI row says so.
  * `statYardage` carries four impossible values (11,131 and 561 yards gained,
    -5,114 and 1,105 on penalties). They are NULLed, never clamped -- a clamped value
    is indistinguishable from a real one -- and `yards_impossible` flags the row so
    they light up in a grid instead of quietly setting a longest-play record.
"""
from __future__ import annotations

import functools
import threading
from pathlib import Path

import duckdb
import pandas as pd

from . import lens

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "out" / "pbp.duckdb"

# A play cannot gain or lose more than the field is long. 100-yard interception
# returns are real -- 45 of them -- so the window sits above them at 110 and catches
# only the four rows that are feed corruption.
YARD_LIMIT = 110

# --------------------------------------------------------------------------- kicks view
# `outcome` collapses the flag columns into one label.
#
# `Unknown` is read straight off the warehouse rather than inferred here. Until
# 2026-08-31 the flags had no NULL state -- a kick whose outcome the parser could not
# read was stored as false on every one of them, indistinguishable from a kick that
# genuinely had no touchback, fair catch or return -- so this view had to reconstruct
# the unreadable set by testing for all-false. st_parser.py now writes NULL on the five
# "how did it end" flags in exactly that case, so `returned IS NULL` is the
# single-column test and the distinction is a fact of the table rather than a
# convention of this file.
#
# `onside` is tested first, ahead of everything else: an onside kick is a different
# play, not a kickoff with an unusual result, and folding it in would both muddy
# touchback rates and dump 1,382 of the 1,486 onside kicks into Unknown. Onside kicks
# keep false (not NULL) flags, so they never collide with the Unknown test.
#
# Conversions are in the view -- all 71,460 of them -- and off by default at the chip.
# See lens.PHASE_DEFAULT for why.
_ST_VIEW = """
CREATE OR REPLACE VIEW st_play AS
SELECT
    p.play_uid,
    p.game_id,
    p.season,
    p.week,
    p.season_type,
    p.play_kind,
    CASE p.play_kind WHEN 'field_goal'           THEN 'Field goal'
                     WHEN 'punt'                 THEN 'Punt'
                     WHEN 'kickoff'              THEN 'Kickoff'
                     WHEN 'pat'                  THEN 'Extra point'
                     WHEN 'two_point'            THEN 'Two-point'
                     WHEN 'defensive_conversion' THEN 'Defensive conv' END     AS phase,
    CASE
      WHEN p.play_kind IN ('pat', 'two_point', 'defensive_conversion') THEN
        CASE WHEN p.converted IS NULL                THEN 'Unknown'
             WHEN p.converted                        THEN 'Converted'
             ELSE 'Failed' END
      WHEN p.play_kind = 'field_goal' THEN
        CASE WHEN p.fg_made IS NULL                  THEN 'Negated'
             WHEN p.fg_made                          THEN 'Made'
             WHEN COALESCE(p.kick_blocked, FALSE)    THEN 'Blocked'
             ELSE 'Missed' END
      ELSE
        CASE WHEN COALESCE(p.onside, FALSE)          THEN 'Onside'
             WHEN p.returned IS NULL                 THEN 'Unknown'
             WHEN COALESCE(p.kick_blocked, FALSE)    THEN 'Blocked'
             WHEN COALESCE(p.touchback, FALSE)       THEN 'Touchback'
             WHEN COALESCE(p.fair_catch, FALSE)      THEN 'Fair catch'
             WHEN COALESCE(p.out_of_bounds, FALSE)   THEN 'Out of bounds'
             WHEN COALESCE(p.downed, FALSE)          THEN 'Downed'
             WHEN p.returned                         THEN 'Returned'
             ELSE 'Unknown' END
    END                                                                      AS outcome,
    outcome = 'Unknown'                                                      AS outcome_unknown,

    -- people (kicking side; this app gives profiles to kickers/punters only)
    p.kicker_athlete_id                                                      AS player_id,
    COALESCE(p.kicker_known_name, p.kicker_name)                             AS player,
    p.kicker_name_confidence                                                 AS player_conf,
    -- The gamebook-clock bug this was built for was fixed upstream on 2026-08-31: 1,012
    -- field goals used to carry the clock and jersey prefix inside the name
    -- ("(09:34) #98 I.Hankins") because parse_field_goal never stripped the clock the
    -- punt and kickoff parsers had stripped since the gamebook dialect was added.
    -- Kept as a standing regression guard, and it is still earning its place: exactly one
    -- row lights up today, from a different cause -- "(Fake Punt) Michael Burton run for
    -- 2 yds" (2014), where the leading parenthetical was captured as the punter. If ESPN
    -- introduces another dialect it shows up in the grid instead of quietly producing
    -- phantom one-kick kickers.
    COALESCE(p.kicker_name LIKE '(%' OR p.kicker_name LIKE '#%', FALSE)
                                                                             AS player_name_unparsed,
    p.returner_name,
    p.returner_athlete_id,
    p.tackler_athlete_id,
    ta.known_name                                                            AS tackler_name,
    p.blocker_name,

    -- teams
    p.kicking_team_id                                                        AS team_id,
    p.kicking_team                                                           AS team,
    p.kicking_conference                                                     AS conference,
    p.kicking_division                                                       AS division,
    p.receiving_team_id                                                      AS opp_id,
    p.receiving_team                                                         AS opponent,
    p.receiving_conference                                                   AS opp_conference,
    p.receiving_division                                                     AS opp_division,
    CASE WHEN p.neutral_site THEN 'Neutral'
         WHEN p.is_home_kicking IS NULL THEN NULL
         WHEN p.is_home_kicking THEN 'Home' ELSE 'Away' END                  AS site,

    -- the kick itself
    p.fg_distance_yds,
    p.punt_gross_yds,
    p.punt_net_yds,
    p.kickoff_yds,
    p.return_yds,
    COALESCE(p.fg_distance_yds, p.punt_gross_yds, p.kickoff_yds)             AS kick_yds,
    CASE WHEN kick_yds IS NULL THEN NULL ELSE (kick_yds / 5)::INT * 5 END    AS kick_bucket,
    p.returned_for_td,
    p.onside,
    p.converted,
    p.two_point_type,
    p.miss_reason,
    p.negated_by_penalty,

    -- situation
    p.period                                                                 AS qtr,
    p.clock_secs_period,
    lpad((p.clock_secs_period / 60)::INT::VARCHAR, 2, '0') || ':' ||
      lpad((p.clock_secs_period % 60)::VARCHAR, 2, '0')                      AS clock,
    p.game_secs_remaining,
    p.down,
    p.distance                                                               AS dist_to_go,
    p.yards_to_goal,
    p.score_diff_kicking                                                     AS score_diff,
    CASE WHEN p.score_diff_kicking > 0 THEN 'Leading'
         WHEN p.score_diff_kicking = 0 THEN 'Tied' ELSE 'Trailing' END       AS score_state,
    p.is_clutch,

    -- environment
    CAST(p.kickoff_utc AS DATE)                                              AS game_date,
    p.venue_name,
    p.venue_city,
    p.venue_state,
    p.surface,
    p.venue_indoor,
    p.attendance,
    p.neutral_site,
    p.conference_game,
    p.fbs_vs_fbs,

    -- provenance
    p.play_text,
    p.parse_confidence,
    p.source
FROM snap.play p
LEFT JOIN snap.dim_athlete ta ON ta.athlete_id = p.tackler_athlete_id
"""

# --------------------------------------------------------------------- scrimmage views
# One template, two subjects. `{subj}` is the team the lens is about and `{other}` the
# side it faced; `{sign}` flips everything measured from the subject's end.
#
# The outcome vocabularies are separate rather than shared because the same event is a
# different fact to each side: a 12-yard completion is a Gain to the offense and a
# First down allowed to the defense, and an interception returned for a score is a
# Turnover TD one way and a Takeaway TD the other. The order of the WHEN branches is
# the specification -- a pick-six sets both is_turnover and is_touchdown, and testing
# the turnover first is what stops it reading as an ordinary offensive touchdown.
_SCRIM_OUTCOME = {
    "off": """
        CASE WHEN s.is_penalty                                       THEN 'Penalty'
             WHEN s.is_turnover AND COALESCE(s.points_scored, 0) < 0 THEN 'Turnover TD'
             WHEN s.is_turnover                                      THEN 'Turnover'
             WHEN COALESCE(s.is_touchdown, FALSE)                    THEN 'Touchdown'
             WHEN s.play_kind = 'sack'                               THEN 'Sack'
             WHEN s.play_kind = 'pass' AND s.is_complete IS FALSE    THEN 'Incomplete'
             WHEN COALESCE(s.first_down_gained, FALSE)               THEN 'First down'
             WHEN yards_gained > 0                                   THEN 'Gain'
             WHEN yards_gained = 0                                   THEN 'No gain'
             WHEN yards_gained < 0                                   THEN 'Loss'
             ELSE 'Unclassified' END
    """,
    "def": """
        CASE WHEN s.is_penalty                                       THEN 'Penalty'
             WHEN s.is_turnover AND COALESCE(s.points_scored, 0) < 0 THEN 'Takeaway TD'
             WHEN s.is_turnover                                      THEN 'Takeaway'
             WHEN COALESCE(s.is_touchdown, FALSE)                    THEN 'TD allowed'
             WHEN s.play_kind = 'sack'                               THEN 'Sack'
             WHEN s.play_kind = 'pass' AND s.is_complete IS FALSE    THEN 'Incomplete'
             WHEN COALESCE(s.first_down_gained, FALSE)               THEN 'First down allowed'
             WHEN yards_gained IS NULL                               THEN 'Unclassified'
             WHEN yards_gained <= 0                                  THEN 'Stop'
             ELSE 'Gain allowed' END
    """,
}

_SCRIM_VIEW = """
CREATE OR REPLACE VIEW {view} AS
SELECT
    s.play_uid,
    s.game_id,
    s.season,
    s.week,
    s.season_type,
    s.play_kind,
    CASE s.play_kind WHEN 'rush'    THEN 'Rush'
                     WHEN 'pass'    THEN 'Pass'
                     WHEN 'sack'    THEN 'Sack'
                     WHEN 'penalty' THEN 'Penalty'
                     ELSE 'Other' END                                        AS phase,
    s.play_type_espn,
    s.drive_id,
    s.drive_number,

    -- the play itself. yards_gained is guarded before anything reads it, so the
    -- outcome label, the charts and every mean agree on one set of values.
    CASE WHEN abs(s.yards_gained) > {limit} THEN NULL
         ELSE s.yards_gained END                                             AS yards_gained,
    COALESCE(abs(s.yards_gained) > {limit}, FALSE)                           AS yards_impossible,
    CASE WHEN yards_gained IS NULL THEN NULL
         ELSE (floor(yards_gained / 5.0) * 5)::INT END                       AS yards_bucket,
    s.first_down_gained,
    s.is_complete,
    s.is_touchdown,
    s.is_turnover,
    s.is_penalty,
    s.is_scoring_play,
    {sign}s.points_scored                                                    AS points_scored,
    s.end_down,
    s.end_distance,
    s.end_yards_to_goal,
    s.end_team_id,
    {outcome}                                                                AS outcome,
    outcome = 'Unclassified'                                                 AS outcome_unknown,

    -- people. The four id columns are ESPN's structured fields; the bridge
    -- snap.scrimmage_athlete carries all twelve roles and every tackler, and is what a
    -- defensive leaderboard has to be built on.
    {player_id}                                                              AS player_id,
    {player}                                                                 AS player,
    NULL::DOUBLE                                                             AS player_conf,
    FALSE                                                                    AS player_name_unparsed,
    s.passer_athlete_id, s.passer_name, s.passer_position,
    s.rusher_athlete_id, s.rusher_name, s.rusher_position,
    s.receiver_athlete_id, s.receiver_name, s.receiver_position,
    s.tackler_athlete_id, s.tackler_name, s.tackler_position,

    -- teams, from the subject's end
    s.{subj}_team_id                                                         AS team_id,
    s.{subj}_team                                                            AS team,
    s.{subj}_conference                                                      AS conference,
    s.{subj}_division                                                        AS division,
    s.{other}_team_id                                                        AS opp_id,
    s.{other}_team                                                           AS opponent,
    s.{other}_conference                                                     AS opp_conference,
    s.{other}_division                                                       AS opp_division,
    -- An unknown home flag is NULL, not 'Away'. 15 rows carry no is_home_offense,
    -- and defaulting them would have put the same team at home on both lenses.
    CASE WHEN s.neutral_site THEN 'Neutral'
         WHEN s.is_home_offense IS NULL THEN NULL
         WHEN {home} THEN 'Home' ELSE 'Away' END                             AS site,

    -- situation
    s.period                                                                 AS qtr,
    s.clock_secs_period,
    lpad((s.clock_secs_period / 60)::INT::VARCHAR, 2, '0') || ':' ||
      lpad((s.clock_secs_period % 60)::VARCHAR, 2, '0')                      AS clock,
    s.game_secs_remaining,
    s.down,
    s.distance                                                               AS dist_to_go,
    s.distance_bucket,
    s.yards_to_goal,
    s.field_zone,
    {sign}s.score_diff_offense                                               AS score_diff,
    CASE WHEN {sign}s.score_diff_offense > 0 THEN 'Leading'
         WHEN {sign}s.score_diff_offense = 0 THEN 'Tied' ELSE 'Trailing' END AS score_state,
    s.is_clutch,

    -- environment
    CAST(s.kickoff_utc AS DATE)                                              AS game_date,
    s.venue_name,
    s.venue_city,
    s.venue_state,
    s.surface,
    s.venue_indoor,
    s.attendance,
    s.neutral_site,
    s.conference_game,
    s.fbs_vs_fbs,

    -- provenance
    s.play_text,
    s.source
FROM snap.scrimmage s
"""

_SUBJECT = {
    "off": {"subj": "offense", "other": "defense", "sign": "",
            "home": "s.is_home_offense",
            "player_id": "COALESCE(s.passer_athlete_id, s.rusher_athlete_id)",
            "player": "COALESCE(s.passer_name, s.rusher_name)"},
    "def": {"subj": "defense", "other": "offense", "sign": "-",
            "home": "NOT s.is_home_offense",
            "player_id": "s.tackler_athlete_id",
            "player": "s.tackler_name"},
}


def _scrim_sql(key: str) -> str:
    return _SCRIM_VIEW.format(view=lens.VIEW[key], limit=YARD_LIMIT,
                              outcome=_SCRIM_OUTCOME[key].strip(), **_SUBJECT[key])


_lock = threading.Lock()
_con: duckdb.DuckDBPyConnection | None = None


def con() -> duckdb.DuckDBPyConnection:
    """Process-wide connection. Callers get a cursor, so this is thread-safe."""
    global _con
    with _lock:
        if _con is None:
            if not DB_PATH.exists():
                raise FileNotFoundError(
                    f"{DB_PATH} not found -- run scripts/build_snapshot.py first"
                )
            c = duckdb.connect(":memory:")
            c.execute(f"ATTACH '{DB_PATH}' AS snap (READ_ONLY)")
            c.execute(_ST_VIEW)
            c.execute(_scrim_sql("off"))
            c.execute(_scrim_sql("def"))
            _con = c
        return _con


def q(sql: str) -> pd.DataFrame:
    return con().cursor().sql(sql).df()


def scalar(sql: str):
    row = con().cursor().sql(sql).fetchone()
    return None if row is None else row[0]


def view(f: dict | None) -> str:
    """The view the current filter state reads."""
    return lens.VIEW[lens.resolve((f or {}).get("lens"))]


# --------------------------------------------------------------------------- SQL literals
def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def in_list(col: str, vals) -> str:
    vals = [v for v in (vals or []) if v not in (None, "")]
    if not vals:
        return "TRUE"
    return f"{col} IN ({', '.join(lit(v) for v in vals)})"


def _any_of(cols: list[str], vals) -> str:
    """One id matched against several role columns -- a receiver is as findable as
    the passer who threw to him."""
    vals = [v for v in (vals or []) if v not in (None, "")]
    if not vals:
        return "TRUE"
    ids = ", ".join(lit(int(v)) for v in vals)
    return "(" + " OR ".join(f"{c} IN ({ids})" for c in cols) + ")"


# The columns a player filter matches, per lens. Offense spans the three offensive
# roles ESPN puts on the row; defense has only the one, and that is the whole reason a
# defensive leaderboard needs the bridge instead.
PLAYER_COLS = {
    "off": ["passer_athlete_id", "rusher_athlete_id", "receiver_athlete_id"],
    "def": ["tackler_athlete_id"],
    "st": ["player_id"],
}


# --------------------------------------------------------------------------- filters
def where_from_filters(f: dict | None, *, ignore: tuple[str, ...] = ()) -> str:
    """Translate the sidebar filter store into a WHERE clause for the current lens.

    `ignore` drops a facet so a chart can show the distribution of the thing the
    user is filtering on without that filter flattening it.
    """
    f = f or {}
    key = lens.resolve(f.get("lens"))
    parts: list[str] = []

    def use(name: str) -> bool:
        return name not in ignore and bool(f.get(name))

    if use("phases"):
        parts.append(in_list("play_kind", lens.kinds_for(key, f["phases"])))
    if f.get("seasons"):
        lo, hi = f["seasons"]
        parts.append(f"season BETWEEN {int(lo)} AND {int(hi)}")
    if use("teams"):
        parts.append(in_list("team_id", [int(t) for t in f["teams"]]))
    if use("opponents"):
        parts.append(in_list("opp_id", [int(t) for t in f["opponents"]]))
    if use("conferences"):
        parts.append(in_list("conference", f["conferences"]))
    if use("divisions"):
        parts.append(in_list("division", f["divisions"]))
    if use("players"):
        parts.append(_any_of(PLAYER_COLS[key], f["players"]))
    if use("outcomes"):
        parts.append(in_list("outcome", f["outcomes"]))
    if use("season_types"):
        parts.append(in_list("season_type", f["season_types"]))
    if use("surfaces"):
        parts.append(in_list("surface", f["surfaces"]))
    if use("qtrs"):
        parts.append(in_list("qtr", [int(x) for x in f["qtrs"]]))
    if use("score_states"):
        parts.append(in_list("score_state", f["score_states"]))

    # Lens-specific facets. Down and field zone are the first two questions anyone
    # asks of a scrimmage table and mean nothing on a kickoff; kick distance is the
    # reverse.
    if lens.is_scrimmage(key):
        if use("downs"):
            parts.append(in_list("down", [int(x) for x in f["downs"]]))
        if use("zones"):
            parts.append(in_list("field_zone", f["zones"]))
    elif f.get("dist"):
        lo, hi = f["dist"]
        blo, bhi = dist_bounds()
        # At full range this filter must be a no-op. Three kickoffs carry an
        # impossible parsed distance (108, 125 and 127 yards), and a hardcoded
        # 0-100 slider was silently dropping them -- and one Unknown-outcome row
        # with them -- from every default count on the page.
        if int(lo) > blo or int(hi) < bhi:
            parts.append(
                f"(kick_yds IS NULL OR kick_yds BETWEEN {int(lo)} AND {int(hi)})")

    if f.get("clutch_only"):
        parts.append("is_clutch")
    if f.get("fbs_only"):
        parts.append("fbs_vs_fbs")
    if f.get("neutral") == "exclude":
        parts.append("NOT neutral_site")
    elif f.get("neutral") == "only":
        parts.append("neutral_site")

    # Roof. Three-state like neutral site, not a value list like surface, because it is
    # a boolean -- and a nullable one: 438 kick and 2,149 scrimmage rows have no venue at
    # all (14 Hawai'i home games in 2019-2020 that carry no venue in the scoreboard), so
    # venue_indoor is NULL there. `IS FALSE` rather than `NOT venue_indoor` states on
    # purpose that those rows leave the selection under "Outdoor only": the feed does not
    # say they were outdoors, and a roof filter that quietly kept unknown-roof rows on the
    # outdoor side would be claiming something it cannot know. Both sides are therefore
    # narrowing, and neither is the complement of the other.
    if f.get("roof") == "indoor":
        parts.append("venue_indoor")
    elif f.get("roof") == "outdoor":
        parts.append("venue_indoor IS FALSE")
    if f.get("conf_game") == "only":
        parts.append("conference_game")
    elif f.get("conf_game") == "exclude":
        parts.append("NOT conference_game")
    if f.get("linked_only"):
        parts.append("player_id IS NOT NULL")

    return " AND ".join(parts) if parts else "TRUE"


def chip_set(f: dict | None) -> list[str]:
    """The phase chips currently selected, in the lens's own order."""
    key = lens.resolve((f or {}).get("lens"))
    sel = set((f or {}).get("phases") or lens.default_chips(key))
    return [c for c in lens.chips(key) if c in sel]


# --------------------------------------------------------------------------- ag grid requests
_NUM_OPS = {"equals": "=", "notEqual": "<>", "greaterThan": ">",
            "greaterThanOrEqual": ">=", "lessThan": "<", "lessThanOrEqual": "<="}


def _one_condition(col: str, c: dict) -> str | None:
    kind, typ = c.get("filterType"), c.get("type")
    if typ == "blank":
        return f"{col} IS NULL"
    if typ == "notBlank":
        return f"{col} IS NOT NULL"

    if kind == "number":
        a, b = c.get("filter"), c.get("filterTo")
        if typ in _NUM_OPS and a is not None:
            return f"{col} {_NUM_OPS[typ]} {float(a)}"
        if typ == "inRange" and a is not None and b is not None:
            return f"{col} BETWEEN {float(a)} AND {float(b)}"
        return None

    if kind == "date":
        a, b = c.get("dateFrom"), c.get("dateTo")
        if typ in _NUM_OPS and a:
            return f"{col} {_NUM_OPS[typ]} DATE {lit(str(a)[:10])}"
        if typ == "inRange" and a and b:
            return f"{col} BETWEEN DATE {lit(str(a)[:10])} AND DATE {lit(str(b)[:10])}"
        return None

    # text
    raw = c.get("filter")
    if raw in (None, ""):
        return None
    v = str(raw).lower().replace("'", "''").replace("%", r"\%").replace("_", r"\_")
    lc = f"lower({col}::VARCHAR)"
    pat = {
        "contains": f"'%{v}%'", "notContains": f"'%{v}%'",
        "equals": f"'{v}'", "notEqual": f"'{v}'",
        "startsWith": f"'{v}%'", "endsWith": f"'%{v}'",
    }.get(typ)
    if pat is None:
        return None
    if typ == "equals":
        return f"{lc} = {pat}"
    if typ == "notEqual":
        return f"({lc} <> {pat} OR {col} IS NULL)"
    neg = "NOT " if typ == "notContains" else ""
    return f"{neg}{lc} LIKE {pat} ESCAPE '\\'"


def filter_model_to_sql(model: dict | None) -> str:
    """AG Grid filterModel -> WHERE fragment. Column filters compose with the sidebar."""
    if not model:
        return "TRUE"
    parts = []
    for col, spec in model.items():
        if not col.replace("_", "").isalnum():
            continue  # never interpolate an unexpected identifier
        conds = spec.get("conditions")
        if conds:
            joiner = " OR " if spec.get("operator") == "OR" else " AND "
            built = [s for s in (_one_condition(col, c) for c in conds) if s]
            if built:
                parts.append("(" + joiner.join(built) + ")")
        else:
            s = _one_condition(col, spec)
            if s:
                parts.append(f"({s})")
    return " AND ".join(parts) if parts else "TRUE"


def sort_model_to_sql(model: list | None, default: str = "game_date DESC") -> str:
    """AG Grid sortModel -> ORDER BY, always tie-broken on play_uid for stable paging."""
    terms = []
    for s in model or []:
        col, direction = s.get("colId", ""), s.get("sort", "asc")
        if col.replace("_", "").isalnum():
            terms.append(f"{col} {'DESC' if direction == 'desc' else 'ASC'} NULLS LAST")
    terms.append(default if not terms else "")
    return ", ".join([t for t in terms if t] + ["play_uid"])


@functools.lru_cache(maxsize=1024)
def count_rows(view_name: str, where: str) -> int:
    return int(scalar(f"SELECT count(*) FROM {view_name} WHERE {where}") or 0)


# --------------------------------------------------------------------------- option lists
@functools.lru_cache(maxsize=1)
def snapshot_meta() -> dict:
    row = con().cursor().sql("SELECT built_at, rows FROM snap.snapshot_meta").fetchone()
    rows = {k: int(scalar(f"SELECT count(*) FROM {lens.VIEW[k]}") or 0)
            for k in lens.KEYS}
    return {"built_at": row[0] if row else None,
            "snapshot_rows": row[1] if row else None,
            "rows": rows,
            # off and def are the same events read twice, so the corpus is the
            # scrimmage fact plus the kicks, not the sum of all three views.
            "total": rows["off"] + rows["st"]}


@functools.lru_cache(maxsize=1)
def season_bounds() -> tuple[int, int]:
    """The corpus window. Both facts span it identically, so one table answers it."""
    lo, hi = con().cursor().sql(
        "SELECT min(season), max(season) FROM snap.season_status").fetchone()
    return int(lo), int(hi)


@functools.lru_cache(maxsize=1)
def in_progress_seasons() -> list[dict]:
    """Seasons still being played, straight off the snapshot's season_status table.

    build_snapshot.py marks a season in progress while its most recent game kicked off
    inside the last 30 days, so nothing here has to be unset once the bowls are over. A
    snapshot built before season_status existed reports none rather than failing.

    This app is descriptive and fits no models, so a part-season is not a correctness
    problem the way it would be for a fitted baseline -- it is a reading problem.
    A team's 2026 row is three games next to twelve full ones, and nothing on the row says
    so. These are the seasons that need saying so.
    """
    try:
        df = q("""SELECT season, games, plays, last_regular_week AS week
                  FROM snap.season_status WHERE is_in_progress ORDER BY season""")
    except Exception:
        return []
    return records(df)


@functools.lru_cache(maxsize=1)
def dist_bounds() -> tuple[int, int]:
    lo, hi = con().cursor().sql(
        "SELECT min(kick_yds), max(kick_yds) FROM st_play").fetchone()
    return int(lo), int(hi)


@functools.lru_cache(maxsize=8)
def team_options(key: str) -> list[dict]:
    df = q(f"""
        SELECT team_id, any_value(team) AS team, count(*) AS n
        FROM {lens.VIEW[lens.resolve(key)]}
        WHERE team_id IS NOT NULL GROUP BY team_id ORDER BY team
    """)
    return [{"value": str(r.team_id), "label": r.team} for r in df.itertuples()
            if isinstance(r.team, str) and r.team]


@functools.lru_cache(maxsize=8)
def conference_options(key: str) -> list[str]:
    return q(f"""SELECT DISTINCT conference FROM {lens.VIEW[lens.resolve(key)]}
                 WHERE conference IS NOT NULL ORDER BY 1""")["conference"].tolist()


@functools.lru_cache(maxsize=8)
def surface_options(key: str) -> list[str]:
    return q(f"""SELECT DISTINCT surface FROM {lens.VIEW[lens.resolve(key)]}
                 WHERE surface IS NOT NULL ORDER BY 1""")["surface"].tolist()


@functools.lru_cache(maxsize=8)
def outcome_options(key: str) -> list[str]:
    return q(f"SELECT DISTINCT outcome FROM {lens.VIEW[lens.resolve(key)]} "
             "ORDER BY 1")["outcome"].tolist()


@functools.lru_cache(maxsize=8)
def zone_options(key: str) -> list[str]:
    """Field zone is a scrimmage column and the facet is hidden on the kicks, so the
    kicks answer with nothing rather than with an error. The control that reads this
    is populated for every lens by one callback; returning [] is what keeps that
    callback lens-blind."""
    key = lens.resolve(key)
    if not lens.is_scrimmage(key):
        return []
    return q(f"""SELECT DISTINCT field_zone FROM {lens.VIEW[key]}
                 WHERE field_zone IS NOT NULL ORDER BY 1""")["field_zone"].tolist()


# A player has to clear this many plays to appear in the picker. Without a floor the
# offense list is ~25,000 names, every one of which ships to the browser on page load.
_PLAYER_FLOOR = {"off": 25, "def": 25, "st": 3}


@functools.lru_cache(maxsize=64)
def player_options(key: str, phases: tuple[str, ...] = ()) -> list[dict]:
    """The players a filter can name, scoped to the selected phases.

    Offense unions the three roles ESPN puts on the row, so a receiver is as findable
    as the passer who threw to him. Defense has only the first-tackler column, which is
    why its list is thin -- see lens.PLAYER_HINT.
    """
    key = lens.resolve(key)
    v = lens.VIEW[key]
    kinds = lens.kinds_for(key, list(phases)) if phases else None
    where = in_list("play_kind", kinds) if kinds else "TRUE"
    floor = _PLAYER_FLOOR[key]

    if key == "off":
        src = " UNION ALL ".join(
            f"SELECT {i} AS athlete_id, {n} AS nm, season FROM {v} "
            f"WHERE {i} IS NOT NULL AND {where}"
            for i, n in (("passer_athlete_id", "passer_name"),
                         ("rusher_athlete_id", "rusher_name"),
                         ("receiver_athlete_id", "receiver_name")))
    elif key == "def":
        src = (f"SELECT tackler_athlete_id AS athlete_id, tackler_name AS nm, season "
               f"FROM {v} WHERE tackler_athlete_id IS NOT NULL AND {where}")
    else:
        src = (f"SELECT player_id AS athlete_id, player AS nm, season "
               f"FROM {v} WHERE player_id IS NOT NULL AND {where}")

    df = q(f"""
        SELECT athlete_id, any_value(nm) AS nm, count(*) AS n,
               min(season) AS s0, max(season) AS s1
        FROM ({src}) t
        GROUP BY athlete_id HAVING count(*) >= {floor} ORDER BY nm
    """)
    return [
        {"value": str(r.athlete_id), "label": f"{r.nm} · {r.s0}-{r.s1} · {r.n:,}"}
        for r in df.itertuples() if isinstance(r.nm, str) and r.nm
    ]


# --------------------------------------------------------------------------- json safety
def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records for AG Grid.

    Dates become ISO strings (AG Grid only needs to *display* them -- all comparison
    happens server-side in SQL), and every flavour of missing value -- NaN, NaT,
    pd.NA -- becomes None so the grid shows an empty cell rather than the string
    "nan".
    """
    if df.empty:
        return []
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            out[col] = s.dt.strftime("%Y-%m-%d")
        elif isinstance(s.dtype, pd.CategoricalDtype):
            out[col] = s.astype("object")
    out = out.astype("object")
    return [
        {k: (None if v is None or v is pd.NA or (isinstance(v, float) and v != v) else v)
         for k, v in row.items()}
        for row in out.to_dict("records")
    ]
