"""Every fixed SQL statement the typed tools run, plus the identifier allow-lists.

Two rules hold throughout this file and are the reason it exists separately from
server.py:

1. **No caller value is ever interpolated into SQL.** Every one is a `%s`
   placeholder bound by the driver. Where a tool genuinely needs to vary an
   *identifier* -- a fact to read, a measure to aggregate -- the identifier comes
   from a module-level allow-list in this file and never from the argument
   itself: `LEAGUES`, `FACTS`, `KICK_KINDS`, `SCRIMMAGE_KINDS`, `SEASON_TYPES`,
   and the keys of `LEADERBOARD_SQL` and `TREND_SQL`. server._one_of() returns the
   ALLOW-LIST's own string, so what reaches SQL is a module constant by
   construction rather than caller text that passed a test.

2. **The domain rules are encoded here, not left to the caller.** This warehouse
   has three traps that produce a plausible wrong number rather than an error,
   and a tool that does not handle them is worse than no tool:

   * **The conference trap.** `dim_team_season` is grained (league, team_id,
     season). A join on team_id alone gets 2018 wrong in both directions. Every
     statement here either reads the wide tables, where the join is already
     resolved, or carries the season.

   * **NULL means something.** `fg_made IS NULL` is a kick wiped out by penalty,
     not a miss. `returned IS NULL` on a punt or kickoff is an outcome the feed
     never stated -- 9.9% of them -- and on a field goal it means the column does
     not apply. A `coalesce(flag, false)` anywhere in this file would recreate
     the exact defect fixed on 2026-08-31, so there is none. Rate denominators
     are stated explicitly and the unstated share is returned alongside, so a
     caller can see how much of the season is actually quotable.

   * **Group by id, label with name.** 109 names are shared by more than one
     athlete, and that is correct -- they are different people. Every
     player-grain statement groups by `athlete_id`.

See README.md "Known limits" for the full list; `known_limits()` in server.py
surfaces the ones a caller is most likely to trip over.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- allow-lists
#
# Identifiers that may be substituted into SQL. A value arriving from a tool
# argument is checked for membership in one of these and the LIST'S OWN STRING is
# what reaches the query -- the argument is never the thing interpolated, so a
# match is proof the text is safe rather than an assertion that it looks safe.

LEAGUES = ("cfb", "nfl")

# fact name -> the wide table it reads. Callers name a fact; they never name a table.
FACTS = {
    "kicks": "pbp.play_wide",
    "scrimmage": "pbp.scrimmage_wide",
}

KICK_KINDS = ("kickoff", "punt", "field_goal", "pat", "two_point", "defensive_conversion")
SCRIMMAGE_KINDS = ("rush", "pass", "sack", "penalty", "other")
SEASON_TYPES = ("regular", "postseason")

# Every table the MCP may introspect or read. Mirrors guard.ALLOWED_TABLES; the
# two are separate lists on purpose (a wrong name is rejected before a connection
# is opened) and selftest.py asserts they agree with the actual grants.
READABLE_TABLES = (
    "play_wide", "scrimmage_wide",
    "special_teams_play", "scrimmage_play", "drive",
    "play_athlete", "scrimmage_athlete",
    "dim_athlete", "dim_team", "dim_team_season", "dim_conference", "dim_venue",
    "fact_game", "season_status",
)

# One line each, for list_schema. The database also carries these as COMMENTs
# (comment_tables.sql) and describe_table reads those; this is the short form.
TABLE_NOTES = {
    "play_wide": "THE KICKS FACT, dimensions pre-joined. Start here for kicking, punting "
                 "and returns. Row count is in approx_rows -- it grows weekly while a "
                 "season is in progress.",
    "scrimmage_wide": "THE SCRIMMAGE FACT, dimensions pre-joined. Rushes, passes, sacks, "
                      "penalties. Disjoint from play_wide; play_uid is unique across both.",
    "special_teams_play": "The normalised kicks fact. play_wide is the same rows with "
                          "dimensions attached; prefer it unless you are auditing.",
    "scrimmage_play": "The normalised scrimmage fact. Prefer scrimmage_wide.",
    "drive": "One row per drive, spanning BOTH facts -- a drive ending in a punt contains "
             "the punt. start_yards_to_goal is from the possessing team's own goal.",
    "play_athlete": "play x role x athlete for the kicks fact. The full truth about who was "
                    "involved; the id columns on the fact are a denormalised hot path.",
    "scrimmage_athlete": "play x role x athlete for the scrimmage fact. 193,215 plays have "
                         "two tacklers.",
    "dim_athlete": "One row per athlete, CAREER-grain, shared across both facts and both "
                   "leagues. 2,779 people appear in both leagues.",
    "dim_team": "One row per (league, team). Team ids COLLIDE between leagues -- team 2 is "
                "Auburn and also the Buffalo Bills.",
    "dim_team_season": "league x team x season, carrying conference. JOIN ON (team_id, "
                       "season), never team_id alone -- 83 teams changed conference in the window.",
    "dim_conference": "One row per (league, conference). Conference ids collide between "
                      "leagues -- 8 is the SEC and also the AFC.",
    "dim_venue": "One row per venue, SHARED across leagues (venue ids are one id space). "
                 "`indoor` is a stadium property, not a game condition.",
    "fact_game": "One row per game. The only declared foreign key in the schema is "
                 "fact_game.venue_id.",
    "season_status": "One row per league-season: games, kicks, last kickoff, and whether the "
                     "season is still being played.",
}

# --------------------------------------------------------------------------- schema
#
# Structure is read through fixed catalog queries filtered by has_table_privilege,
# so a caller sees exactly the tables the role may read and nothing else. This is
# why guard.py can block information_schema outright while describe_table still
# works: the catalogs are reachable from here, never from run_sql.

LIST_SCHEMA = """
SELECT c.relname AS table_name,
       CASE WHEN c.reltuples < 0 THEN NULL
            ELSE c.reltuples::bigint END        AS approx_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
       obj_description(c.oid, 'pg_class')       AS description
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'pbp'
  AND c.relkind IN ('r', 'v', 'm', 'p')
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY c.relname
"""

DESCRIBE_TABLE = """
SELECT a.attname                                   AS column_name,
       format_type(a.atttypid, a.atttypmod)        AS data_type,
       NOT a.attnotnull                            AS nullable,
       col_description(c.oid, a.attnum)            AS description
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = 'pbp'
  AND c.relname = %s
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY a.attnum
"""

TABLE_META = """
SELECT c.relname AS table_name,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS approx_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
       obj_description(c.oid, 'pg_class') AS description
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'pbp' AND c.relname = %s
  AND has_table_privilege(c.oid, 'SELECT')
"""

# --------------------------------------------------------------------------- coverage

DATA_COVERAGE = """
SELECT league, season, games, plays AS kicks, last_regular_week,
       last_kickoff, is_in_progress,
       (now()::date - last_kickoff::date) AS days_since_last_game
FROM pbp.season_status
ORDER BY league, season
"""

CORPUS_TOTALS = """
SELECT league,
       (SELECT count(*) FROM pbp.play_wide      w WHERE w.league = s.league) AS kick_plays,
       (SELECT count(*) FROM pbp.scrimmage_wide w WHERE w.league = s.league) AS scrimmage_plays,
       (SELECT count(*) FROM pbp.fact_game      g WHERE g.league = s.league) AS games,
       (SELECT min(season) FROM pbp.season_status x WHERE x.league = s.league) AS first_season,
       (SELECT max(season) FROM pbp.season_status x WHERE x.league = s.league) AS last_season
FROM (SELECT DISTINCT league FROM pbp.season_status) s
ORDER BY league
"""

# --------------------------------------------------------------------------- discovery

FIND_TEAM = """
SELECT t.league, t.team_id, t.display_name,
       ts.season, ts.conference_name, ts.ncaa_division, ts.nfl_division
FROM pbp.dim_team t
LEFT JOIN pbp.dim_team_season ts
       ON ts.team_id = t.team_id AND ts.league = t.league
      AND ts.season = COALESCE(%(season)s::int, (SELECT max(season) FROM pbp.dim_team_season
                                             WHERE team_id = t.team_id AND league = t.league))
WHERE (%(league)s::text IS NULL OR t.league = %(league)s)
  AND (%(q)s::text IS NULL OR t.display_name ILIKE '%%' || %(q)s || '%%')
-- Relevance, not alphabetical, and DIVISION is the signal that matters.
-- "Alabama" ordered by name answers with "Alabama A&M Bulldogs" -- an FCS
-- programme with 67 plays in the corpus -- ahead of the Crimson Tide, because A
-- sorts before C and both are prefix matches. FCS teams are in dim_team at all
-- only because the corpus deliberately ingests FBS-vs-FCS games, so they are
-- almost never what a question means. Rank: exact name, then top division, then
-- prefix, then the rest.
ORDER BY CASE WHEN %(q)s::text IS NOT NULL
                   AND lower(t.display_name) = lower(%(q)s) THEN 0 ELSE 1 END,
         CASE WHEN ts.ncaa_division = 'FBS' OR ts.nfl_division IS NOT NULL
                   THEN 0 ELSE 1 END,
         CASE WHEN %(q)s::text IS NOT NULL
                   AND t.display_name ILIKE %(q)s || '%%'   THEN 0 ELSE 1 END,
         t.league, t.display_name
LIMIT %(limit)s
"""

# Group by athlete_id, label with known_name -- never the reverse. The `leagues`
# column is what makes a cross-league career visible in one row.
FIND_PLAYER = """
SELECT a.athlete_id, a.known_name, a.full_name, a.position, a.jersey,
       a.primary_role, a.leagues, a.first_season, a.last_season,
       a.st_plays, a.scrimmage_plays, a.nfl_plays,
       t.display_name AS primary_team, t.league AS primary_team_league
FROM pbp.dim_athlete a
LEFT JOIN LATERAL (
    -- Resolve primary_team_id to ONE team. dim_athlete carries no league of its own,
    -- so prefer the league the athlete actually played in; for a cfb+nfl career, or
    -- a tie, fall back to a deterministic order rather than returning both rows.
    SELECT dt.display_name, dt.league
    FROM pbp.dim_team dt
    WHERE dt.team_id = a.primary_team_id
    ORDER BY (dt.league = CASE
        -- primary_team_id is the athlete's MODAL team across the whole career, so
        -- the league it belongs to is whichever league they played more in -- not
        -- simply 'cfb' because the career started there. Patrick Mahomes is
        -- primary_team_id 12 with 7,156 NFL plays against 1,774 college ones: that
        -- is Kansas City. Preferring cfb resolves 12 to the Arizona Wildcats, which
        -- is a different real team and looks entirely plausible.
        WHEN coalesce(a.nfl_plays, 0) * 2
             > coalesce(a.st_plays, 0) + coalesce(a.scrimmage_plays, 0)
        THEN 'nfl' ELSE 'cfb' END) DESC,
             dt.league
    LIMIT 1
) t ON true
WHERE (%(name)s::text     IS NULL OR a.known_name ILIKE '%%' || %(name)s || '%%'
                            OR a.full_name  ILIKE '%%' || %(name)s || '%%')
  AND (%(position)s::text IS NULL OR a.position   ILIKE %(position)s)
  AND (%(league)s::text   IS NULL OR a.leagues     = %(league)s OR a.leagues = 'cfb+nfl')
  AND (%(season)s::int   IS NULL OR (a.first_season <= %(season)s AND a.last_season >= %(season)s))
ORDER BY (a.st_plays + a.scrimmage_plays) DESC NULLS LAST
LIMIT %(limit)s
"""

FIND_VENUE = """
SELECT v.venue_id, v.venue_name, v.city, v.state, v.zip, v.country,
       v.surface, v.indoor,
       (SELECT count(*) FROM pbp.fact_game g WHERE g.venue_id = v.venue_id) AS games
FROM pbp.dim_venue v
WHERE (%(q)s::text      IS NULL OR v.venue_name ILIKE '%%' || %(q)s || '%%'
                          OR v.city       ILIKE '%%' || %(q)s || '%%')
  AND (%(state)s::text  IS NULL OR v.state  = %(state)s)
  AND (%(indoor)s::boolean IS NULL OR v.indoor = %(indoor)s)
ORDER BY games DESC
LIMIT %(limit)s
"""

LIST_CONFERENCES = """
SELECT c.league, c.conference_id, c.conference_name, c.short_name,
       count(DISTINCT ts.team_id) AS teams
FROM pbp.dim_conference c
LEFT JOIN pbp.dim_team_season ts
       ON ts.conference_id = c.conference_id AND ts.league = c.league
      AND (%(season)s::int IS NULL OR ts.season = %(season)s)
WHERE (%(league)s::text IS NULL OR c.league = %(league)s)
GROUP BY c.league, c.conference_id, c.conference_name, c.short_name
ORDER BY c.league, c.conference_name
"""

# --------------------------------------------------------------------------- games

LIST_GAMES = """
SELECT g.game_id, g.league, g.season, g.week, g.season_type, g.kickoff_utc,
       h.display_name AS home_team, g.home_team_id,
       a.display_name AS away_team, g.away_team_id,
       v.venue_name, v.city AS venue_city, v.state AS venue_state, v.indoor AS venue_indoor,
       g.attendance, g.neutral_site, g.conference_game
FROM pbp.fact_game g
LEFT JOIN pbp.dim_team  h ON h.team_id = g.home_team_id AND h.league = g.league
LEFT JOIN pbp.dim_team  a ON a.team_id = g.away_team_id AND a.league = g.league
LEFT JOIN pbp.dim_venue v ON v.venue_id = g.venue_id
WHERE (%(league)s::text      IS NULL OR g.league      = %(league)s)
  AND (%(season)s::int      IS NULL OR g.season      = %(season)s)
  AND (%(week)s::int        IS NULL OR g.week        = %(week)s)
  AND (%(season_type)s::text IS NULL OR g.season_type = %(season_type)s)
  AND (%(team_id)s::int     IS NULL OR g.home_team_id = %(team_id)s OR g.away_team_id = %(team_id)s)
ORDER BY g.kickoff_utc DESC NULLS LAST, g.game_id
LIMIT %(limit)s
"""

GAME_HEADER = """
SELECT g.game_id, g.league, g.season, g.week, g.season_type, g.kickoff_utc,
       h.display_name AS home_team, g.home_team_id,
       a.display_name AS away_team, g.away_team_id,
       hs.conference_name AS home_conference, as_.conference_name AS away_conference,
       v.venue_name, v.city AS venue_city, v.state AS venue_state,
       v.surface, v.indoor AS venue_indoor,
       g.attendance, g.neutral_site, g.conference_game
FROM pbp.fact_game g
LEFT JOIN pbp.dim_team  h ON h.team_id = g.home_team_id AND h.league = g.league
LEFT JOIN pbp.dim_team  a ON a.team_id = g.away_team_id AND a.league = g.league
LEFT JOIN pbp.dim_team_season hs
       ON hs.team_id = g.home_team_id AND hs.season = g.season AND hs.league = g.league
LEFT JOIN pbp.dim_team_season as_
       ON as_.team_id = g.away_team_id AND as_.season = g.season AND as_.league = g.league
LEFT JOIN pbp.dim_venue v ON v.venue_id = g.venue_id
WHERE g.game_id = %s
"""

# The warehouse stores no final score -- it is a play-level corpus. This derives one
# the same way reports/margins.py does: sum the signed points each side gained.
# `points_scored` on the scrimmage fact is from the OFFENCE's perspective and is
# negative on a pick-six, so a plain sum per team is already correct.
GAME_SCORE = """
WITH scrim AS (
    -- ONLY the positive rows. points_scored is signed from the OFFENCE's perspective,
    -- so a pick-six is -6 on the offence's row -- meaning "the defence scored 6", not
    -- "the offence lost 6". Summing it unfiltered deducts those points from a team that
    -- simply failed to score, and the game total comes out low by 6 for every one.
    SELECT offense_team_id AS team_id, sum(points_scored) AS pts
    FROM pbp.scrimmage_wide
    WHERE game_id = %(game_id)s AND points_scored > 0
    GROUP BY 1
    UNION ALL
    -- the other side of the same rows: flip the negative onto the team that did score
    SELECT defense_team_id, -sum(points_scored)
    FROM pbp.scrimmage_wide
    WHERE game_id = %(game_id)s AND points_scored < 0
    GROUP BY 1
), kicks AS (
    SELECT kicking_team_id AS team_id,
           sum(CASE WHEN play_kind = 'field_goal' AND fg_made       THEN 3
                    WHEN play_kind = 'pat'        AND converted     THEN 1
                    WHEN play_kind = 'two_point'  AND converted     THEN 2
                    ELSE 0 END) AS pts
    FROM pbp.play_wide
    WHERE game_id = %(game_id)s
    GROUP BY 1
    UNION ALL
    -- a kick returned for a touchdown scores for the RECEIVING team
    SELECT receiving_team_id, 6 * count(*)
    FROM pbp.play_wide
    WHERE game_id = %(game_id)s AND returned_for_td
    GROUP BY 1
)
SELECT t.team_id, d.display_name AS team, sum(t.pts)::int AS points
FROM (SELECT * FROM scrim UNION ALL SELECT * FROM kicks) t
LEFT JOIN pbp.fact_game g ON g.game_id = %(game_id)s
LEFT JOIN pbp.dim_team  d ON d.team_id = t.team_id AND d.league = g.league
WHERE t.team_id IS NOT NULL
GROUP BY t.team_id, d.display_name
ORDER BY points DESC
"""

GAME_PLAY_COUNTS = """
SELECT 'kicks' AS fact, play_kind, count(*) AS plays
FROM pbp.play_wide WHERE game_id = %(game_id)s GROUP BY 2
UNION ALL
SELECT 'scrimmage', play_kind, count(*)
FROM pbp.scrimmage_wide WHERE game_id = %(game_id)s GROUP BY 2
ORDER BY 1, 3 DESC
"""

DRIVE_CHART = """
SELECT d.drive_number, d.drive_id, o.display_name AS offense, d.offense_team_id,
       d.result, d.display_result, d.is_score,
       d.start_period, d.start_clock_secs, d.start_yards_to_goal, d.start_text,
       d.end_period, d.end_clock_secs, d.end_yards_to_goal, d.end_text,
       d.plays_total, d.plays_scrimmage, d.yards, d.time_elapsed_secs
FROM pbp.drive d
LEFT JOIN pbp.dim_team o ON o.team_id = d.offense_team_id AND o.league = d.league
WHERE d.game_id = %s
ORDER BY d.drive_number
"""

# --------------------------------------------------------------------------- play detail

PLAY_DETAIL_KICKS = "SELECT * FROM pbp.play_wide WHERE play_uid = %s"
PLAY_DETAIL_SCRIMMAGE = "SELECT * FROM pbp.scrimmage_wide WHERE play_uid = %s"

# Everyone who touched one play. Both bridges are checked because a play_uid is
# unique across BOTH facts by construction, so exactly one of these returns rows.
PLAY_PARTICIPANTS = """
SELECT b.role, b.ordinal, b.athlete_id, a.known_name, a.position, a.primary_role
FROM pbp.play_athlete b
LEFT JOIN pbp.dim_athlete a ON a.athlete_id = b.athlete_id
WHERE b.play_uid = %(play_uid)s
UNION ALL
SELECT b.role, b.ordinal, b.athlete_id, a.known_name, a.position, a.primary_role
FROM pbp.scrimmage_athlete b
LEFT JOIN pbp.dim_athlete a ON a.athlete_id = b.athlete_id
WHERE b.play_uid = %(play_uid)s
ORDER BY role, ordinal
"""

# --------------------------------------------------------------------------- players

PLAYER_PROFILE = """
SELECT a.athlete_id, a.known_name, a.full_name, a.position, a.jersey,
       a.text_name, a.text_name_confidence, a.primary_role, a.leagues,
       a.first_season, a.last_season, a.st_plays, a.scrimmage_plays, a.nfl_plays,
       a.date_of_birth, a.debut_year, a.height_in, a.weight_lb,
       t.display_name AS primary_team, t.league AS primary_team_league
FROM pbp.dim_athlete a
LEFT JOIN LATERAL (
    -- Resolve primary_team_id to ONE team. dim_athlete carries no league of its own,
    -- so prefer the league the athlete actually played in; for a cfb+nfl career, or
    -- a tie, fall back to a deterministic order rather than returning both rows.
    SELECT dt.display_name, dt.league
    FROM pbp.dim_team dt
    WHERE dt.team_id = a.primary_team_id
    ORDER BY (dt.league = CASE
        -- primary_team_id is the athlete's MODAL team across the whole career, so
        -- the league it belongs to is whichever league they played more in -- not
        -- simply 'cfb' because the career started there. Patrick Mahomes is
        -- primary_team_id 12 with 7,156 NFL plays against 1,774 college ones: that
        -- is Kansas City. Preferring cfb resolves 12 to the Arizona Wildcats, which
        -- is a different real team and looks entirely plausible.
        WHEN coalesce(a.nfl_plays, 0) * 2
             > coalesce(a.st_plays, 0) + coalesce(a.scrimmage_plays, 0)
        THEN 'nfl' ELSE 'cfb' END) DESC,
             dt.league
    LIMIT 1
) t ON true
WHERE a.athlete_id = %s
"""

# Career kicking, split by league and season so a cross-league career reads as two
# blocks rather than one blended number. fg_made IS NULL is a negated kick and is
# excluded from the denominator rather than counted as a miss.
PLAYER_KICKING = """
SELECT league, season,
       count(*) FILTER (WHERE play_kind = 'field_goal' AND fg_made IS NOT NULL) AS fg_att,
       count(*) FILTER (WHERE play_kind = 'field_goal' AND fg_made)             AS fg_made,
       round(100.0 * avg(CASE WHEN play_kind = 'field_goal' AND fg_made IS NOT NULL
                              THEN fg_made::int END), 1)                        AS fg_pct,
       max(fg_distance_yds) FILTER (WHERE play_kind = 'field_goal' AND fg_made) AS longest_fg,
       count(*) FILTER (WHERE play_kind = 'pat' AND converted IS NOT NULL)      AS pat_att,
       count(*) FILTER (WHERE play_kind = 'pat' AND converted)                  AS pat_made,
       count(*) FILTER (WHERE play_kind = 'punt')                               AS punts,
       round(avg(punt_gross_yds) FILTER (WHERE play_kind = 'punt'), 2)          AS punt_gross_avg,
       round(avg(punt_net_yds)   FILTER (WHERE play_kind = 'punt'), 2)          AS punt_net_avg,
       count(*) FILTER (WHERE play_kind = 'kickoff')                            AS kickoffs,
       round(100.0 * avg(CASE WHEN play_kind = 'kickoff' AND returned IS NOT NULL
                              THEN touchback::int END), 1)                      AS kickoff_tb_pct
FROM pbp.play_wide
WHERE kicker_athlete_id = %(athlete_id)s
  AND (%(season)s::int IS NULL OR season = %(season)s)
GROUP BY league, season
ORDER BY league, season
"""

# Career scrimmage production. Turnovers are held out of the mean-yards measures
# because ESPN's statYardage on a turnover is the DEFENCE's return, not the
# offence's gain -- it credits 35 yards to the offence row of a 35-yard pick-six.
PLAYER_SCRIMMAGE = """
SELECT league, season,
       count(*) FILTER (WHERE rusher_athlete_id = %(athlete_id)s
                          AND play_kind = 'rush')                        AS rush_att,
       sum(yards_gained) FILTER (WHERE rusher_athlete_id = %(athlete_id)s
                          AND play_kind = 'rush' AND NOT is_turnover)     AS rush_yds,
       round(avg(yards_gained) FILTER (WHERE rusher_athlete_id = %(athlete_id)s
                          AND play_kind = 'rush' AND NOT is_turnover), 2) AS rush_ypc,
       count(*) FILTER (WHERE rusher_athlete_id = %(athlete_id)s
                          AND is_touchdown)                               AS rush_td,

       count(*) FILTER (WHERE passer_athlete_id = %(athlete_id)s
                          AND play_kind = 'pass')                         AS pass_att,
       count(*) FILTER (WHERE passer_athlete_id = %(athlete_id)s
                          AND is_complete)                                AS pass_cmp,
       sum(yards_gained) FILTER (WHERE passer_athlete_id = %(athlete_id)s
                          AND play_kind = 'pass' AND NOT is_turnover)      AS pass_yds,
       count(*) FILTER (WHERE passer_athlete_id = %(athlete_id)s
                          AND is_touchdown)                                AS pass_td,
       count(*) FILTER (WHERE passer_athlete_id = %(athlete_id)s
                          AND is_turnover)                                 AS pass_int,

       count(*) FILTER (WHERE receiver_athlete_id = %(athlete_id)s)         AS targets,
       count(*) FILTER (WHERE receiver_athlete_id = %(athlete_id)s
                          AND is_complete)                                  AS receptions,
       sum(yards_gained) FILTER (WHERE receiver_athlete_id = %(athlete_id)s
                          AND is_complete AND NOT is_turnover)              AS rec_yds,

       count(*) FILTER (WHERE tackler_athlete_id = %(athlete_id)s)          AS tackles
FROM pbp.scrimmage_wide
WHERE (%(athlete_id)s IN (passer_athlete_id, rusher_athlete_id,
                          receiver_athlete_id, tackler_athlete_id))
  AND (%(season)s::int IS NULL OR season = %(season)s)
GROUP BY league, season
ORDER BY league, season
"""

PLAYER_GAME_LOG = """
SELECT w.game_id, w.league, w.season, w.week, w.season_type, w.kickoff_utc,
       w.offense_team AS team, w.defense_team AS opponent,
       count(*) FILTER (WHERE w.rusher_athlete_id = %(athlete_id)s)   AS rush_att,
       sum(w.yards_gained) FILTER (WHERE w.rusher_athlete_id = %(athlete_id)s
                                     AND NOT w.is_turnover)            AS rush_yds,
       count(*) FILTER (WHERE w.passer_athlete_id = %(athlete_id)s)    AS pass_att,
       count(*) FILTER (WHERE w.passer_athlete_id = %(athlete_id)s
                          AND w.is_complete)                           AS pass_cmp,
       sum(w.yards_gained) FILTER (WHERE w.passer_athlete_id = %(athlete_id)s
                                     AND NOT w.is_turnover)            AS pass_yds,
       count(*) FILTER (WHERE w.receiver_athlete_id = %(athlete_id)s
                          AND w.is_complete)                           AS receptions,
       sum(w.yards_gained) FILTER (WHERE w.receiver_athlete_id = %(athlete_id)s
                                     AND w.is_complete AND NOT w.is_turnover) AS rec_yds,
       count(*) FILTER (WHERE w.tackler_athlete_id = %(athlete_id)s)   AS tackles,
       count(*) FILTER (WHERE w.is_touchdown
                          AND %(athlete_id)s IN (w.rusher_athlete_id, w.receiver_athlete_id,
                                                 w.passer_athlete_id)) AS touchdowns
FROM pbp.scrimmage_wide w
WHERE %(athlete_id)s IN (w.passer_athlete_id, w.rusher_athlete_id,
                         w.receiver_athlete_id, w.tackler_athlete_id)
  AND (%(season)s::int IS NULL OR w.season = %(season)s)
GROUP BY w.game_id, w.league, w.season, w.week, w.season_type, w.kickoff_utc,
         w.offense_team, w.defense_team
ORDER BY w.kickoff_utc DESC NULLS LAST
LIMIT %(limit)s
"""

# --------------------------------------------------------------------------- leaderboards
#
# One statement per measure. They are separate rather than one parameterised
# statement because the DENOMINATOR differs per measure and that is exactly the
# thing this warehouse punishes getting wrong -- a shared template would have to
# take the denominator as a parameter, which is the bug it is meant to prevent.
#
# `min_attempts` is a parameter on every one: a 100% field-goal kicker with two
# attempts is not the leader in anything, and an unqualified leaderboard is the
# most common way to produce a confidently wrong answer here.

LEADERBOARD_SQL: dict[str, str] = {
    "fg_pct": """
        SELECT w.kicker_athlete_id AS athlete_id, a.known_name, a.position,
               max(w.kicking_team) AS team, w.league,
               count(*)                                AS attempts,
               count(*) FILTER (WHERE w.fg_made)       AS made,
               round(100.0 * avg(w.fg_made::int), 1)   AS fg_pct,
               max(w.fg_distance_yds) FILTER (WHERE w.fg_made) AS longest
        FROM pbp.play_wide w
        JOIN pbp.dim_athlete a ON a.athlete_id = w.kicker_athlete_id
        WHERE w.play_kind = 'field_goal'
          AND w.fg_made IS NOT NULL          -- a negated kick is not a miss
          AND (%(league)s::text IS NULL OR w.league = %(league)s)
          AND (%(season)s::int IS NULL OR w.season = %(season)s)
          AND (NOT %(top_division_only)s::boolean OR w.both_top_division)
        GROUP BY w.kicker_athlete_id, a.known_name, a.position, w.league
        HAVING count(*) >= %(min_attempts)s
        ORDER BY fg_pct DESC, attempts DESC
        LIMIT %(limit)s
    """,
    "punt_gross": """
        SELECT w.kicker_athlete_id AS athlete_id, a.known_name, a.position,
               max(w.kicking_team) AS team, w.league,
               count(*)                          AS attempts,
               round(avg(w.punt_gross_yds), 2)   AS punt_gross_avg,
               round(avg(w.punt_net_yds), 2)     AS punt_net_avg
        FROM pbp.play_wide w
        JOIN pbp.dim_athlete a ON a.athlete_id = w.kicker_athlete_id
        WHERE w.play_kind = 'punt' AND w.punt_gross_yds IS NOT NULL
          AND (%(league)s::text IS NULL OR w.league = %(league)s)
          AND (%(season)s::int IS NULL OR w.season = %(season)s)
          AND (NOT %(top_division_only)s::boolean OR w.both_top_division)
        GROUP BY w.kicker_athlete_id, a.known_name, a.position, w.league
        HAVING count(*) >= %(min_attempts)s
        ORDER BY punt_gross_avg DESC
        LIMIT %(limit)s
    """,
    "rush_yards": """
        SELECT w.rusher_athlete_id AS athlete_id, a.known_name, a.position,
               max(w.offense_team) AS team, w.league,
               count(*)                                        AS attempts,
               sum(w.yards_gained) FILTER (WHERE NOT w.is_turnover) AS yards,
               round(avg(w.yards_gained) FILTER (WHERE NOT w.is_turnover), 2) AS ypc,
               count(*) FILTER (WHERE w.is_touchdown)          AS touchdowns
        FROM pbp.scrimmage_wide w
        JOIN pbp.dim_athlete a ON a.athlete_id = w.rusher_athlete_id
        WHERE w.play_kind = 'rush'
          AND (%(league)s::text IS NULL OR w.league = %(league)s)
          AND (%(season)s::int IS NULL OR w.season = %(season)s)
          AND (NOT %(top_division_only)s::boolean OR w.both_top_division)
        GROUP BY w.rusher_athlete_id, a.known_name, a.position, w.league
        HAVING count(*) >= %(min_attempts)s
        ORDER BY yards DESC NULLS LAST
        LIMIT %(limit)s
    """,
    "pass_yards": """
        SELECT w.passer_athlete_id AS athlete_id, a.known_name, a.position,
               max(w.offense_team) AS team, w.league,
               count(*)                                        AS attempts,
               count(*) FILTER (WHERE w.is_complete)           AS completions,
               round(100.0 * avg(w.is_complete::int), 1)       AS completion_pct,
               sum(w.yards_gained) FILTER (WHERE NOT w.is_turnover) AS yards,
               round(avg(w.yards_gained) FILTER (WHERE NOT w.is_turnover), 2) AS ypa,
               count(*) FILTER (WHERE w.is_touchdown)          AS touchdowns,
               count(*) FILTER (WHERE w.is_turnover)           AS interceptions
        FROM pbp.scrimmage_wide w
        JOIN pbp.dim_athlete a ON a.athlete_id = w.passer_athlete_id
        WHERE w.play_kind = 'pass'
          AND (%(league)s::text IS NULL OR w.league = %(league)s)
          AND (%(season)s::int IS NULL OR w.season = %(season)s)
          AND (NOT %(top_division_only)s::boolean OR w.both_top_division)
        GROUP BY w.passer_athlete_id, a.known_name, a.position, w.league
        HAVING count(*) >= %(min_attempts)s
        ORDER BY yards DESC NULLS LAST
        LIMIT %(limit)s
    """,
    "receiving_yards": """
        SELECT w.receiver_athlete_id AS athlete_id, a.known_name, a.position,
               max(w.offense_team) AS team, w.league,
               count(*)                                   AS targets,
               count(*) FILTER (WHERE w.is_complete)      AS receptions,
               sum(w.yards_gained) FILTER (WHERE w.is_complete AND NOT w.is_turnover) AS yards,
               count(*) FILTER (WHERE w.is_touchdown)     AS touchdowns
        FROM pbp.scrimmage_wide w
        JOIN pbp.dim_athlete a ON a.athlete_id = w.receiver_athlete_id
        WHERE w.receiver_athlete_id IS NOT NULL
          AND (%(league)s::text IS NULL OR w.league = %(league)s)
          AND (%(season)s::int IS NULL OR w.season = %(season)s)
          AND (NOT %(top_division_only)s::boolean OR w.both_top_division)
        GROUP BY w.receiver_athlete_id, a.known_name, a.position, w.league
        HAVING count(*) >= %(min_attempts)s
        ORDER BY yards DESC NULLS LAST
        LIMIT %(limit)s
    """,
    "tackles": """
        SELECT b.athlete_id, a.known_name, a.position,
               max(w.defense_team) AS team, w.league,
               count(*) AS tackles,
               count(*) FILTER (WHERE b.role = 'tackler')    AS solo,
               count(*) FILTER (WHERE b.role = 'assistedBy') AS assisted,
               count(*) FILTER (WHERE b.role = 'sackedBy')   AS sacks
        FROM pbp.scrimmage_athlete b
        JOIN pbp.scrimmage_wide w ON w.play_uid = b.play_uid
        JOIN pbp.dim_athlete    a ON a.athlete_id = b.athlete_id
        WHERE b.role IN ('tackler', 'assistedBy', 'sackedBy')
          AND (%(league)s::text IS NULL OR w.league = %(league)s)
          AND (%(season)s::int IS NULL OR w.season = %(season)s)
          AND (NOT %(top_division_only)s::boolean OR w.both_top_division)
        GROUP BY b.athlete_id, a.known_name, a.position, w.league
        HAVING count(*) >= %(min_attempts)s
        ORDER BY tackles DESC
        LIMIT %(limit)s
    """,
}

# --------------------------------------------------------------------------- analysis

# Monotonic in both leagues over 30k and 12.8k attempts -- if this comes back
# non-monotonic, something upstream is wrong, not the football.
FG_BY_DISTANCE = """
SELECT league, fg_dist_bucket,
       CASE WHEN fg_dist_bucket = 15 THEN '<20'
            WHEN fg_dist_bucket = 60 THEN '60+'
            ELSE fg_dist_bucket || '-' || (fg_dist_bucket + 4) END AS distance_band,
       count(*)                              AS attempts,
       count(*) FILTER (WHERE fg_made)       AS made,
       round(100.0 * avg(fg_made::int), 1)   AS fg_pct
FROM pbp.play_wide
WHERE play_kind = 'field_goal'
  AND fg_made IS NOT NULL            -- negated kicks are not misses
  AND fg_dist_bucket IS NOT NULL
  AND (%(league)s::text IS NULL OR league = %(league)s)
  AND (%(season)s::int IS NULL OR season = %(season)s)
  AND (NOT %(top_division_only)s::boolean OR both_top_division)
GROUP BY league, fg_dist_bucket
ORDER BY league, fg_dist_bucket
"""

# Rates on the denominator they belong on. `returned IS NULL` is an outcome the
# feed never stated -- it is EXCLUDED from the rates and reported separately as
# pct_unstated, because averaging over it silently understates every rate.
# `onside` is excluded from kickoff rates: it is a different play, not an outcome.
KICK_OUTCOMES = """
SELECT league, season, play_kind,
       count(*)                                       AS kicks,
       count(*) FILTER (WHERE returned IS NULL)       AS unstated,
       round(100.0 * avg((returned IS NULL)::int), 1) AS pct_unstated,
       count(*) FILTER (WHERE returned IS NOT NULL)   AS classified,
       round(100.0 * avg(touchback::int), 1)          AS touchback_pct,
       round(100.0 * avg(returned::int), 1)           AS returned_pct,
       round(100.0 * avg(fair_catch::int), 1)         AS fair_catch_pct,
       round(100.0 * avg(out_of_bounds::int), 1)      AS out_of_bounds_pct,
       round(avg(return_yds) FILTER (WHERE returned), 1) AS avg_return_yds,
       count(*) FILTER (WHERE returned_for_td)        AS return_tds
FROM pbp.play_wide
WHERE play_kind = ANY(%(kinds)s::text[])
  AND NOT onside                      -- a different play, not an outcome
  AND (%(league)s::text IS NULL OR league = %(league)s)
  AND (%(season)s::int IS NULL OR season = %(season)s)
  AND (NOT %(top_division_only)s::boolean OR both_top_division)
GROUP BY league, season, play_kind
ORDER BY league, play_kind, season
"""

# Drive outcome by where the drive started. Monotonic in both leagues; a field
# position column reversed anywhere upstream produces this curve backwards.
DRIVE_OUTCOMES = """
SELECT d.league,
       CASE WHEN d.start_yards_to_goal <= 20 THEN '01 inside opp 20'
            WHEN d.start_yards_to_goal <= 40 THEN '02 opp 20-40'
            WHEN d.start_yards_to_goal <= 60 THEN '03 midfield'
            WHEN d.start_yards_to_goal <= 80 THEN '04 own 20-40'
            ELSE                                  '05 inside own 20' END AS start_zone,
       count(*)                                          AS drives,
       round(100.0 * avg((d.result = 'TD')::int), 1)     AS td_pct,
       round(100.0 * avg((d.result = 'FG')::int), 1)     AS fg_pct,
       round(100.0 * avg((d.result = 'PUNT')::int), 1)   AS punt_pct,
       round(100.0 * avg(d.is_score::int), 1)            AS score_pct,
       round(avg(d.yards), 1)                            AS avg_yards,
       round(avg(d.plays_total), 1)                      AS avg_plays
FROM pbp.drive d
WHERE d.start_yards_to_goal IS NOT NULL
  AND d.start_yards_to_goal > 0       -- 0 is a null sentinel, not the goal line
  AND (%(league)s::text IS NULL OR d.league = %(league)s)
  AND (%(season)s::int IS NULL OR d.season = %(season)s)
GROUP BY d.league, start_zone
ORDER BY d.league, start_zone
"""

# Down-and-distance efficiency. Turnovers are held out of the yards mean for the
# statYardage reason documented at the top of this file.
SITUATIONAL_SPLITS = """
SELECT league, down, distance_bucket, field_zone,
       count(*)                                              AS plays,
       round(100.0 * avg(first_down_gained::int), 1)         AS first_down_pct,
       round(avg(yards_gained) FILTER (WHERE NOT is_turnover), 2) AS avg_yards,
       round(100.0 * avg(is_touchdown::int), 2)              AS td_pct,
       round(100.0 * avg(is_turnover::int), 2)               AS turnover_pct,
       round(100.0 * avg((play_kind = 'pass')::int), 1)      AS pass_rate
FROM pbp.scrimmage_wide
WHERE play_kind IN ('rush', 'pass', 'sack')
  AND down BETWEEN 1 AND 4
  AND (%(league)s::text IS NULL OR league = %(league)s)
  AND (%(season)s::int IS NULL OR season = %(season)s)
  AND (NOT %(top_division_only)s::boolean OR both_top_division)
GROUP BY league, down, distance_bucket, field_zone
ORDER BY league, down, distance_bucket, field_zone
"""

# One measure by season, for trend questions. Each entry states its own
# denominator; see the note on LEADERBOARD_SQL.
TREND_SQL: dict[str, str] = {
    "fg_pct": """
        SELECT league, season, count(*) AS n,
               round(100.0 * avg(fg_made::int), 2) AS value
        FROM pbp.play_wide
        WHERE play_kind = 'field_goal' AND fg_made IS NOT NULL
          AND (%(league)s::text IS NULL OR league = %(league)s)
          AND (NOT %(top_division_only)s::boolean OR both_top_division)
        GROUP BY league, season ORDER BY league, season
    """,
    "kickoff_touchback_pct": """
        SELECT league, season, count(*) FILTER (WHERE returned IS NOT NULL) AS n,
               round(100.0 * avg(touchback::int), 2) AS value
        FROM pbp.play_wide
        WHERE play_kind = 'kickoff' AND NOT onside
          AND (%(league)s::text IS NULL OR league = %(league)s)
          AND (NOT %(top_division_only)s::boolean OR both_top_division)
        GROUP BY league, season ORDER BY league, season
    """,
    "punt_gross_avg": """
        SELECT league, season, count(*) AS n,
               round(avg(punt_gross_yds), 2) AS value
        FROM pbp.play_wide
        WHERE play_kind = 'punt' AND punt_gross_yds IS NOT NULL
          AND (%(league)s::text IS NULL OR league = %(league)s)
          AND (NOT %(top_division_only)s::boolean OR both_top_division)
        GROUP BY league, season ORDER BY league, season
    """,
    "pat_pct": """
        SELECT league, season, count(*) AS n,
               round(100.0 * avg(converted::int), 2) AS value
        FROM pbp.play_wide
        WHERE play_kind = 'pat' AND converted IS NOT NULL
          AND (%(league)s::text IS NULL OR league = %(league)s)
          AND (NOT %(top_division_only)s::boolean OR both_top_division)
        GROUP BY league, season ORDER BY league, season
    """,
    "completion_pct": """
        SELECT league, season, count(*) AS n,
               round(100.0 * avg(is_complete::int), 2) AS value
        FROM pbp.scrimmage_wide
        WHERE play_kind = 'pass' AND is_complete IS NOT NULL
          AND (%(league)s::text IS NULL OR league = %(league)s)
          AND (NOT %(top_division_only)s::boolean OR both_top_division)
        GROUP BY league, season ORDER BY league, season
    """,
    "yards_per_carry": """
        SELECT league, season, count(*) AS n,
               round(avg(yards_gained), 3) AS value
        FROM pbp.scrimmage_wide
        WHERE play_kind = 'rush' AND NOT is_turnover
          AND (%(league)s::text IS NULL OR league = %(league)s)
          AND (NOT %(top_division_only)s::boolean OR both_top_division)
        GROUP BY league, season ORDER BY league, season
    """,
    "yards_per_pass_attempt": """
        SELECT league, season, count(*) AS n,
               round(avg(yards_gained), 3) AS value
        FROM pbp.scrimmage_wide
        WHERE play_kind = 'pass' AND NOT is_turnover
          AND (%(league)s::text IS NULL OR league = %(league)s)
          AND (NOT %(top_division_only)s::boolean OR both_top_division)
        GROUP BY league, season ORDER BY league, season
    """,
    # Named for what it measures. It is the share of drives that ended in ANY score
    # (drive.is_score), not points per drive -- there is no points column on a drive,
    # and calling it that would have been a wrong answer with a confident label.
    "drive_score_pct": """
        SELECT league, season, count(*) AS n,
               round(100.0 * avg(is_score::int), 2) AS value
        FROM pbp.drive
        WHERE (%(league)s::text IS NULL OR league = %(league)s)
        GROUP BY league, season ORDER BY league, season
    """,
}

# --------------------------------------------------------------------------- team

TEAM_SEASON = """
SELECT %(team_id)s AS team_id, %(league)s AS league, %(season)s AS season,
       (SELECT display_name FROM pbp.dim_team
         WHERE team_id = %(team_id)s AND league = %(league)s)  AS team,
       (SELECT conference_name FROM pbp.dim_team_season
         WHERE team_id = %(team_id)s AND league = %(league)s
           AND season = %(season)s)                            AS conference,
       o.plays AS off_plays, o.yards AS off_yards, o.ypp AS off_ypp,
       o.pass_rate, o.first_down_pct AS off_first_down_pct, o.td AS off_td,
       o.turnovers AS off_turnovers,
       d.plays AS def_plays, d.yards AS def_yards, d.ypp AS def_ypp,
       d.first_down_pct AS def_first_down_pct, d.td AS def_td,
       d.turnovers AS def_takeaways,
       k.fg_att, k.fg_made, k.fg_pct, k.punts, k.punt_gross_avg
FROM (SELECT count(*) AS plays,
             sum(yards_gained) FILTER (WHERE NOT is_turnover)        AS yards,
             round(avg(yards_gained) FILTER (WHERE NOT is_turnover), 2) AS ypp,
             round(100.0 * avg((play_kind = 'pass')::int), 1)        AS pass_rate,
             round(100.0 * avg(first_down_gained::int), 1)           AS first_down_pct,
             count(*) FILTER (WHERE is_touchdown)                    AS td,
             count(*) FILTER (WHERE is_turnover)                     AS turnovers
      FROM pbp.scrimmage_wide
      WHERE offense_team_id = %(team_id)s AND league = %(league)s AND season = %(season)s
        AND play_kind IN ('rush','pass','sack')) o
CROSS JOIN
     (SELECT count(*) AS plays,
             sum(yards_gained) FILTER (WHERE NOT is_turnover)        AS yards,
             round(avg(yards_gained) FILTER (WHERE NOT is_turnover), 2) AS ypp,
             round(100.0 * avg(first_down_gained::int), 1)           AS first_down_pct,
             count(*) FILTER (WHERE is_touchdown)                    AS td,
             count(*) FILTER (WHERE is_turnover)                     AS turnovers
      FROM pbp.scrimmage_wide
      WHERE defense_team_id = %(team_id)s AND league = %(league)s AND season = %(season)s
        AND play_kind IN ('rush','pass','sack')) d
CROSS JOIN
     (SELECT count(*) FILTER (WHERE play_kind = 'field_goal' AND fg_made IS NOT NULL) AS fg_att,
             count(*) FILTER (WHERE play_kind = 'field_goal' AND fg_made)             AS fg_made,
             round(100.0 * avg(CASE WHEN play_kind = 'field_goal' AND fg_made IS NOT NULL
                                    THEN fg_made::int END), 1)                        AS fg_pct,
             count(*) FILTER (WHERE play_kind = 'punt')                               AS punts,
             round(avg(punt_gross_yds) FILTER (WHERE play_kind = 'punt'), 2)          AS punt_gross_avg
      FROM pbp.play_wide
      WHERE kicking_team_id = %(team_id)s AND league = %(league)s AND season = %(season)s) k
"""

# --------------------------------------------------------------------------- quality

PARSE_QUALITY = """
SELECT league, season, play_kind,
       count(*)                                                      AS plays,
       count(*) FILTER (WHERE parse_confidence = 'exact')            AS exact,
       round(100.0 * avg((parse_confidence = 'exact')::int), 2)      AS pct_exact,
       count(*) FILTER (WHERE parse_confidence IS DISTINCT FROM 'exact') AS not_exact,
       count(*) FILTER (WHERE kicker_athlete_id IS NULL)             AS no_kicker_id
FROM pbp.play_wide
WHERE (%(league)s::text IS NULL OR league = %(league)s)
  AND (%(season)s::int IS NULL OR season = %(season)s)
GROUP BY league, season, play_kind
ORDER BY league, season, play_kind
"""

AUDIT_PLAYS = """
SELECT play_uid, league, season, play_kind, kicking_team, kicker_name,
       kicker_known_name, parse_confidence, play_text
FROM pbp.play_wide
WHERE parse_confidence IS DISTINCT FROM 'exact'
  AND (%(league)s::text IS NULL OR league = %(league)s)
  AND (%(season)s::int IS NULL OR season = %(season)s)
  AND (%(play_kind)s::text IS NULL OR play_kind = %(play_kind)s)
ORDER BY season DESC, play_uid
LIMIT %(limit)s
"""
