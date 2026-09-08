"""Materialise one wide analytic table out of Postgres into a local DuckDB file.

The app reads only this file, so it starts instantly and keeps working when Postgres is
down. DuckDB's postgres extension does the extract, so there is no ORM and no driver
dependency -- ATTACH, then one CREATE TABLE AS.

Every join to `dim_team_season` is on (team_id, season), never team_id alone. 83 of 275 teams
changed conference at least once in the window, and a team-only join gets 2018 wrong in both
directions -- 492 Pac-12 punts against 730, and 1,128 Big Ten against 881. Note the size of
the error drifts with the present day: the same wrong query returned 108 Pac-12 punts while
the corpus ended in 2025 and the conference was down to two members.

    python scripts/build_snapshot.py            -> data/out/st.duckdb
    python scripts/build_snapshot.py --db cfb   -> different source database
"""
import argparse, os, sys, time

import duckdb

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HOME, "data", "out", "st.duckdb")

# One row per special teams play, dimensions flattened on.
PLAY_SQL = """
CREATE OR REPLACE TABLE play AS
SELECT
    p.play_uid, p.source, p.game_id, p.season, p.week, p.season_type, p.play_kind,

    -- situation
    p.period, p.clock_secs_period, p.wallclock_utc, p.down, p.distance, p.yards_to_goal,
    p.kicking_team_id, p.receiving_team_id, p.is_home_kicking, p.score_diff_kicking,

    -- outcome
    p.fg_distance_yds, p.fg_made, p.punt_gross_yds, p.punt_net_yds, p.kickoff_yds,
    p.return_yds, p.returned, p.touchback, p.onside, p.fair_catch, p.downed,
    p.out_of_bounds, p.kick_blocked, p.returned_for_td, p.converted, p.two_point_type,
    p.miss_reason, p.negated_by_penalty,

    -- people
    p.kicker_athlete_id, p.returner_athlete_id, p.tackler_athlete_id,
    p.kicker_name, p.returner_name, p.blocker_name,
    ka.known_name      AS kicker_known_name,
    ka.name_confidence AS kicker_name_confidence,


    -- game / venue
    g.kickoff_utc, g.attendance, p.neutral_site, p.conference_game,
    p.venue_id, v.venue_name, v.city AS venue_city, v.state AS venue_state,
    v.country AS venue_country, v.surface,

    -- team-season identity (realignment-safe)
    kt.display_name AS kicking_team, rt.display_name AS receiving_team,
    kts.conference_name AS kicking_conference, kts.division AS kicking_division,
    rts.conference_name AS receiving_conference, rts.division AS receiving_division,

    -- provenance
    p.play_text, p.parse_confidence,

    -- ---- derived, computed once here so the app never recomputes them ----
    -- seconds left in regulation; overtime collapses to 0
    CASE WHEN p.period IS NULL OR p.clock_secs_period IS NULL THEN NULL
         WHEN p.period > 4 THEN 0
         ELSE (4 - p.period) * 900 + p.clock_secs_period END AS game_secs_remaining,
    -- 4th quarter or later, one score either way, under five minutes
    (p.period >= 4 AND abs(p.score_diff_kicking) <= 8
        AND (p.period > 4 OR p.clock_secs_period <= 300)) AS is_clutch,
    -- 5-yard bucket, keyed by its lower bound so it sorts numerically; <20 and 60+ are open
    CASE WHEN p.fg_distance_yds IS NULL THEN NULL
         WHEN p.fg_distance_yds < 20 THEN 15
         WHEN p.fg_distance_yds >= 60 THEN 60
         ELSE (p.fg_distance_yds // 5) * 5 END AS fg_dist_bucket,
    month(g.kickoff_utc) AS game_month,
    -- both teams FBS, i.e. exclude the FBS-vs-FCS games PLAN.md §8 chose to ingest anyway
    (kts.division = 'FBS' AND rts.division = 'FBS') AS fbs_vs_fbs
FROM pg.st.special_teams_play p
LEFT JOIN pg.st.fact_game       g   ON g.game_id = p.game_id
LEFT JOIN pg.st.dim_venue       v   ON v.venue_id = p.venue_id
LEFT JOIN pg.st.dim_team        kt  ON kt.team_id = p.kicking_team_id
LEFT JOIN pg.st.dim_team        rt  ON rt.team_id = p.receiving_team_id
LEFT JOIN pg.st.dim_team_season kts ON kts.team_id = p.kicking_team_id   AND kts.season = p.season
LEFT JOIN pg.st.dim_team_season rts ON rts.team_id = p.receiving_team_id AND rts.season = p.season
LEFT JOIN pg.st.dim_athlete     ka  ON ka.athlete_id = p.kicker_athlete_id
"""

# One row per season, so both apps can tell a season that is still being played from one
# that is finished without either of them hardcoding a year. The rule is deliberately
# self-maintaining: a season is in progress while its most recent game is recent. Nothing
# has to be unset in January -- roughly a month after the last bowl, 2026 stops being
# in progress on its own. The offseason correctly reports no season in progress at all.
IN_PROGRESS_DAYS = 30

SEASON_STATUS_SQL = f"""
CREATE OR REPLACE TABLE season_status AS
SELECT season,
       count(DISTINCT game_id)                                   AS games,
       count(*)                                                  AS plays,
       max(week) FILTER (WHERE season_type = 'regular')           AS last_regular_week,
       max(kickoff_utc)                                          AS last_kickoff,
       max(kickoff_utc) > now() - INTERVAL {IN_PROGRESS_DAYS} DAY AS is_in_progress
FROM play
GROUP BY season
ORDER BY season
"""

COPIES = {
    "dim_athlete":     "SELECT * FROM pg.st.dim_athlete",
    "dim_team_season": "SELECT * FROM pg.st.dim_team_season",
    "dim_venue":       "SELECT * FROM pg.st.dim_venue",
    "fact_game":       "SELECT * FROM pg.st.fact_game",
    "play_athlete":    "SELECT * FROM pg.st.play_athlete",
}


def build(db: str, out: str) -> None:
    if os.path.exists(out):
        os.remove(out)
    con = duckdb.connect(out)
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{'dbname=' + db}' AS pg (TYPE POSTGRES, READ_ONLY)")

    t0 = time.time()
    con.execute(PLAY_SQL)
    n = con.execute("SELECT count(*) FROM play").fetchone()[0]
    print(f"play              {n:>9,} rows   {time.time() - t0:5.1f}s")

    con.execute(SEASON_STATUS_SQL)
    live = con.execute("SELECT season, games, plays FROM season_status "
                       "WHERE is_in_progress ORDER BY season").fetchall()
    print("season_status            "
          + (", ".join(f"{y} IN PROGRESS ({g:,} games, {pl:,} plays)" for y, g, pl in live)
             if live else "no season in progress"))

    for name, sql in COPIES.items():
        con.execute(f"CREATE OR REPLACE TABLE {name} AS {sql}")
        c = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"{name:<18}{c:>9,} rows")

    # Stamp the snapshot so the app can show its age rather than pretend it is live.
    con.execute("CREATE OR REPLACE TABLE snapshot_meta AS "
                "SELECT now() AS built_at, ? AS source_db, ? AS rows", [db, n])
    con.execute("DETACH pg")
    con.close()
    print(f"\n{out}  ({os.path.getsize(out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="cfb")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    try:
        build(a.db, a.out)
    except duckdb.Error as e:
        sys.exit(f"snapshot failed: {e}")
