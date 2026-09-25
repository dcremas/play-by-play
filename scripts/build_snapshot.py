"""Materialise one wide analytic table out of Postgres into a local DuckDB file.

The app reads only this file, so it starts instantly and keeps working when Postgres is
down. DuckDB's postgres extension does the extract, so there is no ORM and no driver
dependency -- ATTACH, then one CREATE TABLE AS.

Every join to `dim_team_season` is on (team_id, season), never team_id alone. 83 of 275 teams
changed conference at least once in the window, and a team-only join gets 2018 wrong in both
directions -- 492 Pac-12 punts against 730, and 1,128 Big Ten against 881. Note the size of
the error drifts with the present day: the same wrong query returned 108 Pac-12 punts while
the corpus ended in 2025 and the conference was down to two members.

    python scripts/build_snapshot.py            -> data/out/pbp.duckdb
    python scripts/build_snapshot.py --db pbp   -> different source database

Two facts since 2026-09-08: `play` is special teams, `scrimmage` is everything else, and the
two are disjoint with play_uid unique across both. `drive` spans them.

`venue_indoor` is a stadium property, not a game condition: a retractable roof reads true
whether or not it was open that day, and 5 of the 18 indoor venues are retractable. It is
not a substitute for "weather did not affect this play".
"""
import argparse, os, sys, time

import duckdb

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HOME, "data", "out", "pbp.duckdb")

# One row per special teams play, dimensions flattened on.
PLAY_SQL = """
CREATE OR REPLACE TABLE play AS
SELECT
    p.play_uid, p.league, p.source, p.game_id, p.season, p.week, p.season_type, p.play_kind,

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
    p.snapper_name, p.holder_name,
    ka.known_name           AS kicker_known_name,
    ka.text_name_confidence AS kicker_name_confidence,
    ka.position             AS kicker_position,


    -- game / venue
    g.kickoff_utc, g.attendance, p.neutral_site, p.conference_game,
    p.venue_id, v.venue_name, v.city AS venue_city, v.state AS venue_state,
    v.country AS venue_country, v.surface, v.indoor AS venue_indoor,

    -- team-season identity (realignment-safe)
    kt.display_name AS kicking_team, rt.display_name AS receiving_team,
    kts.conference_name AS kicking_conference, kts.ncaa_division AS kicking_ncaa_division,
    kts.nfl_division   AS kicking_nfl_division,
    rts.conference_name AS receiving_conference, rts.ncaa_division AS receiving_ncaa_division,
    rts.nfl_division   AS receiving_nfl_division,

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
    -- both teams FBS, i.e. exclude the FBS-vs-FCS games PLAN.md §8 chose to ingest anyway.
    -- CONSTANT TRUE for the NFL, which has no second division: every NFL game is between two
    -- top-flight teams. Leaving it to the college expression would evaluate NULL there and
    -- any filter using it would silently drop the entire NFL corpus.
    CASE WHEN p.league = 'nfl' THEN true
         ELSE (kts.ncaa_division = 'FBS' AND rts.ncaa_division = 'FBS') END AS both_top_division
-- EVERY team join carries the league. NFL team ids run 1-34 and collide outright with
-- college ids -- team 2 is Auburn and also the Buffalo Bills -- so a league-blind join does
-- not merely mislabel a team, it matches TWO dimension rows per play and doubles the table.
-- dim_venue and dim_athlete are joined WITHOUT a league because their ids are a single
-- shared space; that asymmetry is the whole design and is spelled out in sql/migrate_league.sql.
FROM pg.pbp.special_teams_play p
LEFT JOIN pg.pbp.fact_game       g   ON g.game_id = p.game_id AND g.league = p.league
LEFT JOIN pg.pbp.dim_venue       v   ON v.venue_id = p.venue_id
LEFT JOIN pg.pbp.dim_team        kt  ON kt.team_id = p.kicking_team_id   AND kt.league = p.league
LEFT JOIN pg.pbp.dim_team        rt  ON rt.team_id = p.receiving_team_id AND rt.league = p.league
LEFT JOIN pg.pbp.dim_team_season kts ON kts.team_id = p.kicking_team_id   AND kts.season = p.season AND kts.league = p.league
LEFT JOIN pg.pbp.dim_team_season rts ON rts.team_id = p.receiving_team_id AND rts.season = p.season AND rts.league = p.league
LEFT JOIN pg.pbp.dim_athlete     ka  ON ka.athlete_id = p.kicker_athlete_id
"""

# One row per scrimmage play, dimensions flattened on -- the mirror of PLAY_SQL above.
#
# The apps do not read this table yet (PLAN.md §10j.4 keeps them special-teams-only until the
# data has been queried directly), but it belongs in the snapshot regardless: the whole point
# of deferring the UI decision was to be able to query the new fact offline, and the snapshot
# is what "offline" means here.
#
# Team naming joins dim_team_season on (team_id, season), never team_id alone, for the same
# realignment reason spelled out above.
SCRIMMAGE_SQL = """
CREATE OR REPLACE TABLE scrimmage AS
SELECT
    p.play_uid, p.league, p.source, p.game_id, p.season, p.week, p.season_type,
    p.play_kind, p.play_type_espn, p.drive_id, p.drive_number,

    -- situation
    p.period, p.clock_secs_period, p.wallclock_utc, p.down, p.distance, p.yards_to_goal,
    p.offense_team_id, p.defense_team_id, p.is_home_offense, p.score_diff_offense,

    -- outcome
    p.yards_gained, p.end_down, p.end_distance, p.end_yards_to_goal, p.end_team_id,
    p.first_down_gained, p.is_complete, p.is_touchdown, p.is_turnover, p.is_penalty,
    p.is_scoring_play, p.points_scored,

    -- people
    p.passer_athlete_id, p.rusher_athlete_id, p.receiver_athlete_id, p.tackler_athlete_id,
    pa.known_name AS passer_name,   pa.position AS passer_position,
    ra.known_name AS rusher_name,   ra.position AS rusher_position,
    wa.known_name AS receiver_name, wa.position AS receiver_position,
    ta.known_name AS tackler_name,  ta.position AS tackler_position,

    -- game / venue
    g.kickoff_utc, g.attendance, p.neutral_site, p.conference_game,
    p.venue_id, v.venue_name, v.city AS venue_city, v.state AS venue_state,
    v.country AS venue_country, v.surface, v.indoor AS venue_indoor,

    -- team-season identity (realignment-safe)
    ot.display_name AS offense_team, dt.display_name AS defense_team,
    ots.conference_name AS offense_conference, ots.ncaa_division AS offense_ncaa_division,
    ots.nfl_division   AS offense_nfl_division,
    dts.conference_name AS defense_conference, dts.ncaa_division AS defense_ncaa_division,
    dts.nfl_division   AS defense_nfl_division,

    p.play_text,

    -- ---- derived, computed once here so the app never recomputes them ----
    CASE WHEN p.period IS NULL OR p.clock_secs_period IS NULL THEN NULL
         WHEN p.period > 4 THEN 0
         ELSE (4 - p.period) * 900 + p.clock_secs_period END AS game_secs_remaining,
    (p.period >= 4 AND abs(p.score_diff_offense) <= 8
        AND (p.period > 4 OR p.clock_secs_period <= 300)) AS is_clutch,
    -- the standard down-and-distance buckets, so every consumer cuts them the same way
    CASE WHEN p.down IS NULL OR p.distance IS NULL THEN NULL
         WHEN p.distance <= 3 THEN 'short'
         WHEN p.distance <= 7 THEN 'medium'
         ELSE 'long' END AS distance_bucket,
    CASE WHEN p.yards_to_goal IS NULL THEN NULL
         WHEN p.yards_to_goal <= 20 THEN 'red zone'
         WHEN p.yards_to_goal <= 50 THEN 'opponent half'
         ELSE 'own half' END AS field_zone,
    month(g.kickoff_utc) AS game_month,
    -- Constant true for the NFL; see the note in PLAY_SQL.
    CASE WHEN p.league = 'nfl' THEN true
         ELSE (ots.ncaa_division = 'FBS' AND dts.ncaa_division = 'FBS') END AS both_top_division
-- Same league-carrying joins as PLAY_SQL; see the note there.
FROM pg.pbp.scrimmage_play p
LEFT JOIN pg.pbp.fact_game       g   ON g.game_id = p.game_id AND g.league = p.league
LEFT JOIN pg.pbp.dim_venue       v   ON v.venue_id = p.venue_id
LEFT JOIN pg.pbp.dim_team        ot  ON ot.team_id = p.offense_team_id AND ot.league = p.league
LEFT JOIN pg.pbp.dim_team        dt  ON dt.team_id = p.defense_team_id AND dt.league = p.league
LEFT JOIN pg.pbp.dim_team_season ots ON ots.team_id = p.offense_team_id AND ots.season = p.season AND ots.league = p.league
LEFT JOIN pg.pbp.dim_team_season dts ON dts.team_id = p.defense_team_id AND dts.season = p.season AND dts.league = p.league
LEFT JOIN pg.pbp.dim_athlete     pa  ON pa.athlete_id = p.passer_athlete_id
LEFT JOIN pg.pbp.dim_athlete     ra  ON ra.athlete_id = p.rusher_athlete_id
LEFT JOIN pg.pbp.dim_athlete     wa  ON wa.athlete_id = p.receiver_athlete_id
LEFT JOIN pg.pbp.dim_athlete     ta  ON ta.athlete_id = p.tackler_athlete_id
"""

# One row per season, so both apps can tell a season that is still being played from one
# that is finished without either of them hardcoding a year. The rule is deliberately
# self-maintaining: a season is in progress while its most recent game is recent. Nothing
# has to be unset in January -- roughly a month after the last bowl, 2026 stops being
# in progress on its own. The offseason correctly reports no season in progress at all.
IN_PROGRESS_DAYS = 30

SEASON_STATUS_SQL = f"""
CREATE OR REPLACE TABLE season_status AS
SELECT league, season,
       count(DISTINCT game_id)                                   AS games,
       count(*)                                                  AS plays,
       max(week) FILTER (WHERE season_type = 'regular')           AS last_regular_week,
       max(kickoff_utc)                                          AS last_kickoff,
       max(kickoff_utc) > now() - INTERVAL {IN_PROGRESS_DAYS} DAY AS is_in_progress
FROM play
GROUP BY league, season
ORDER BY league, season
"""

COPIES = {
    "dim_athlete":       "SELECT * FROM pg.pbp.dim_athlete",
    "dim_team":          "SELECT * FROM pg.pbp.dim_team",
    "dim_team_season":   "SELECT * FROM pg.pbp.dim_team_season",
    "dim_venue":         "SELECT * FROM pg.pbp.dim_venue",
    "fact_game":         "SELECT * FROM pg.pbp.fact_game",
    "play_athlete":      "SELECT * FROM pg.pbp.play_athlete",
    "scrimmage_athlete": "SELECT * FROM pg.pbp.scrimmage_athlete",
    "drive":             "SELECT * FROM pg.pbp.drive",
}


def build(db: str, out: str) -> None:
    if os.path.exists(out):
        os.remove(out)
    con = duckdb.connect(out)
    con.execute("INSTALL postgres; LOAD postgres;")
    # The scanner parallelises a table read by opening one COPY stream per ctid range, up to
    # pg_connection_limit (64 by default). At 316k rows that is fine; at 1.5M it exhausted the
    # machine's socket buffers and Postgres killed the transfer with "No buffer space
    # available" / "connection to client lost". The extract is I/O bound on one local disk
    # anyway, so the cap costs nothing measurable.
    con.execute("SET pg_connection_limit = 4")
    con.execute(f"ATTACH '{'dbname=' + db}' AS pg (TYPE POSTGRES, READ_ONLY)")

    t0 = time.time()
    con.execute(PLAY_SQL)
    n = con.execute("SELECT count(*) FROM play").fetchone()[0]
    print(f"play              {n:>9,} rows   {time.time() - t0:5.1f}s")

    t1 = time.time()
    con.execute(SCRIMMAGE_SQL)
    ns = con.execute("SELECT count(*) FROM scrimmage").fetchone()[0]
    print(f"scrimmage         {ns:>9,} rows   {time.time() - t1:5.1f}s")

    con.execute(SEASON_STATUS_SQL)
    live = con.execute("SELECT league, season, games, plays FROM season_status "
                       "WHERE is_in_progress ORDER BY league, season").fetchall()
    print("season_status            "
          + (", ".join(f"{ln} {y} IN PROGRESS ({g:,} games, {pl:,} plays)"
                       for ln, y, g, pl in live)
             if live else "no season in progress"))

    for name, sql in COPIES.items():
        con.execute(f"CREATE OR REPLACE TABLE {name} AS {sql}")
        c = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"{name:<18}{c:>9,} rows")

    # Stamp the snapshot so the app can show its age rather than pretend it is live.
    con.execute("CREATE OR REPLACE TABLE snapshot_meta AS "
                "SELECT now() AS built_at, ? AS source_db, ? AS rows, ? AS scrimmage_rows",
                [db, n, ns])
    con.execute("DETACH pg")
    con.close()
    print(f"\n{out}  ({os.path.getsize(out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="pbp")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    try:
        build(a.db, a.out)
    except duckdb.Error as e:
        sys.exit(f"snapshot failed: {e}")
