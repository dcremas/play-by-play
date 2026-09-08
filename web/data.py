"""DuckDB access layer for the special teams instance explorer.

Scope is the three kicking phases the app covers -- field goals, punts and kickoffs
(242,868 of the snapshot's 313,583 rows). PATs and two-point tries are excluded.

That exclusion is worth revisiting. Half its original justification was that PAT rows
carried no kicker identity at all; that was fixed on 2026-08-31 and conversions now link
at 98.6%. What still holds is that they are ~71k attempts at one distance.

The snapshot is opened READ_ONLY and attached to an in-memory database, so the view
below is created without writing to the file and the Streamlit console can keep
using it at the same time.
"""
from __future__ import annotations

import functools
import threading
from pathlib import Path

import duckdb
import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "out" / "pbp.duckdb"

PHASES = {"field_goal": "Field goal", "punt": "Punt", "kickoff": "Kickoff"}
PHASE_ORDER = ["field_goal", "punt", "kickoff"]

# --------------------------------------------------------------------------- view
# `outcome` collapses the flag columns into one label.
#
# `Unknown` is now read straight off the warehouse rather than inferred here. Until
# 2026-08-31 the flags had no NULL state -- a kick whose outcome the parser could not read
# was stored as false on every one of them, indistinguishable from a kick that genuinely
# had no touchback, fair catch or return -- so this view had to reconstruct the unreadable
# set by testing for all-false. st_parser.py now writes NULL on the five "how did it end"
# flags in exactly that case, so `returned IS NULL` is the single-column test and the
# distinction is a fact of the table rather than a convention of this file.
#
# `onside` is tested first, ahead of everything else: an onside kick is a different play,
# not a kickoff with an unusual result, and folding it in would both muddy touchback rates
# and dump 1,382 of the 1,486 onside kicks into Unknown. Onside kicks keep false (not NULL)
# flags, so they never collide with the Unknown test.
_VIEW = """
CREATE OR REPLACE VIEW st AS
SELECT
    p.play_uid,
    p.game_id,
    p.season,
    p.week,
    p.season_type,
    p.play_kind,
    CASE p.play_kind WHEN 'field_goal' THEN 'Field goal'
                     WHEN 'punt'       THEN 'Punt'
                     WHEN 'kickoff'    THEN 'Kickoff' END                    AS phase,
    CASE
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
WHERE p.play_kind IN ('field_goal', 'punt', 'kickoff')
"""

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
            c.execute(_VIEW)
            _con = c
        return _con


def q(sql: str) -> pd.DataFrame:
    return con().cursor().sql(sql).df()


def scalar(sql: str):
    row = con().cursor().sql(sql).fetchone()
    return None if row is None else row[0]


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


# --------------------------------------------------------------------------- filters
def where_from_filters(f: dict | None, *, ignore: tuple[str, ...] = ()) -> str:
    """Translate the sidebar filter store into a WHERE clause.

    `ignore` drops a facet so a chart can show the distribution of the thing the
    user is filtering on without that filter flattening it.
    """
    f = f or {}
    parts: list[str] = []

    def use(key: str) -> bool:
        return key not in ignore and bool(f.get(key))

    if use("phases"):
        parts.append(in_list("play_kind", f["phases"]))
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
        parts.append(in_list("player_id", [int(p) for p in f["players"]]))
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
    if f.get("dist"):
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
    if f.get("conf_game") == "only":
        parts.append("conference_game")
    elif f.get("conf_game") == "exclude":
        parts.append("NOT conference_game")
    if f.get("linked_only"):
        parts.append("player_id IS NOT NULL")

    return " AND ".join(parts) if parts else "TRUE"


def phase_set(f: dict | None) -> set[str]:
    sel = (f or {}).get("phases") or PHASE_ORDER
    return set(sel)


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


@functools.lru_cache(maxsize=512)
def count_rows(where: str) -> int:
    return int(scalar(f"SELECT count(*) FROM st WHERE {where}") or 0)


# --------------------------------------------------------------------------- option lists
@functools.lru_cache(maxsize=1)
def snapshot_meta() -> dict:
    row = con().cursor().sql("SELECT built_at, rows FROM snap.snapshot_meta").fetchone()
    n = int(scalar("SELECT count(*) FROM st") or 0)
    return {"built_at": row[0] if row else None, "snapshot_rows": row[1] if row else None,
            "rows": n}


@functools.lru_cache(maxsize=1)
def season_bounds() -> tuple[int, int]:
    lo, hi = con().cursor().sql("SELECT min(season), max(season) FROM st").fetchone()
    return int(lo), int(hi)


@functools.lru_cache(maxsize=1)
def in_progress_seasons() -> list[dict]:
    """Seasons still being played, straight off the snapshot's season_status table.

    build_snapshot.py marks a season in progress while its most recent game kicked off
    inside the last 30 days, so nothing here has to be unset once the bowls are over. A
    snapshot built before season_status existed reports none rather than failing.

    This app is descriptive and fits no models, so a part-season is not a correctness
    problem the way it is for the Streamlit console's baselines -- it is a reading problem.
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
        "SELECT min(kick_yds), max(kick_yds) FROM st").fetchone()
    return int(lo), int(hi)


@functools.lru_cache(maxsize=1)
def team_options() -> list[dict]:
    df = q("""
        SELECT team_id, any_value(team) AS team, count(*) n
        FROM st GROUP BY team_id ORDER BY team
    """)
    return [{"value": str(r.team_id), "label": r.team} for r in df.itertuples()]


@functools.lru_cache(maxsize=1)
def conference_options() -> list[str]:
    return q("""SELECT DISTINCT conference FROM st
                WHERE conference IS NOT NULL ORDER BY 1""")["conference"].tolist()


@functools.lru_cache(maxsize=1)
def surface_options() -> list[str]:
    return q("""SELECT DISTINCT surface FROM st
                WHERE surface IS NOT NULL ORDER BY 1""")["surface"].tolist()


@functools.lru_cache(maxsize=1)
def outcome_options() -> list[str]:
    return q("SELECT DISTINCT outcome FROM st ORDER BY 1")["outcome"].tolist()


@functools.lru_cache(maxsize=64)
def player_options(phases: tuple[str, ...] = ()) -> list[dict]:
    """Kickers/punters with a linked athlete id, labelled with their volume."""
    where = in_list("play_kind", list(phases)) if phases else "TRUE"
    df = q(f"""
        SELECT player_id, any_value(player) AS player, count(*) n,
               min(season) s0, max(season) s1
        FROM st WHERE player_id IS NOT NULL AND {where}
        GROUP BY player_id HAVING count(*) >= 3
        ORDER BY player
    """)
    return [
        {"value": str(r.player_id),
         "label": f"{r.player} · {r.s0}-{r.s1} · {r.n:,}"}
        for r in df.itertuples()
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
