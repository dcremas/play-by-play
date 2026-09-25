-- The serving layer for the MCP server: the two facts with their dimensions flattened on,
-- materialised as real tables, plus season_status.
--
--     psql -d pbp -f sql/wide_tables.sql
--
-- WHY THIS FILE EXISTS
-- --------------------
-- These are the Postgres twins of the `play`, `scrimmage` and `season_status` tables that
-- scripts/build_snapshot.py builds inside data/out/pbp.duckdb. The column lists are kept
-- deliberately identical, so a query written against the DuckDB snapshot runs unchanged
-- against the warehouse and vice versa. IF YOU CHANGE ONE, CHANGE THE OTHER -- the two
-- drifting apart is the single most likely way for this pair to start lying.
--
-- They are MATERIALISED rather than left as views for one measured reason: the EC2 box has
-- 2 vCPU and 640 MB of shared_buffers against a 2.4 GB scrimmage fact. A view re-runs seven
-- joins on every question, and the common aggregate queries were tripping the role's
-- statement timeout. A table costs ~2 GB of disk (there is 16 GB free) and turns those same
-- queries into a single scan.
--
-- THIS IS A DERIVED SERVING LAYER, NOT PART OF THE SYSTEM OF RECORD. The warehouse is still
-- the 11 tables in README.md's data dictionary. Drop these and rebuild rather than
-- repairing them; nothing upstream reads them.
--
-- Re-run after ANY load. They are a point-in-time projection and do not self-maintain, so a
-- weekly update_season.py run leaves them stale until this file runs again. scripts/sync_ec2.py
-- does that automatically; a hand-run load does not.

\set ON_ERROR_STOP on
\timing on

-- ---------------------------------------------------------------- play_wide (kicks)
--
-- EVERY team join carries the league. NFL team ids run 1-34 and collide outright with
-- college ids -- team 2 is Auburn and also the Buffalo Bills -- so a league-blind join does
-- not merely mislabel a team, it matches TWO dimension rows per play and doubles the table.
-- dim_venue and dim_athlete are joined WITHOUT a league because their ids are a single
-- shared space; that asymmetry is the whole design and is spelled out in sql/migrate_league.sql.
DROP TABLE IF EXISTS pbp.play_wide;
CREATE TABLE pbp.play_wide AS
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

    -- ---- derived, computed once here so no consumer recomputes them ----
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
         ELSE (p.fg_distance_yds / 5) * 5 END AS fg_dist_bucket,
    EXTRACT(MONTH FROM g.kickoff_utc)::smallint AS game_month,
    -- both teams FBS, i.e. exclude the FBS-vs-FCS games PLAN.md section 8 chose to ingest
    -- anyway. CONSTANT TRUE for the NFL, which has no second division: every NFL game is
    -- between two top-flight teams. Leaving it to the college expression would evaluate NULL
    -- there and any filter using it would silently drop the entire NFL corpus.
    CASE WHEN p.league = 'nfl' THEN true
         ELSE (kts.ncaa_division = 'FBS' AND rts.ncaa_division = 'FBS') END AS both_top_division
FROM pbp.special_teams_play p
LEFT JOIN pbp.fact_game       g   ON g.game_id = p.game_id AND g.league = p.league
LEFT JOIN pbp.dim_venue       v   ON v.venue_id = p.venue_id
LEFT JOIN pbp.dim_team        kt  ON kt.team_id = p.kicking_team_id   AND kt.league = p.league
LEFT JOIN pbp.dim_team        rt  ON rt.team_id = p.receiving_team_id AND rt.league = p.league
LEFT JOIN pbp.dim_team_season kts ON kts.team_id = p.kicking_team_id   AND kts.season = p.season AND kts.league = p.league
LEFT JOIN pbp.dim_team_season rts ON rts.team_id = p.receiving_team_id AND rts.season = p.season AND rts.league = p.league
LEFT JOIN pbp.dim_athlete     ka  ON ka.athlete_id = p.kicker_athlete_id;

ALTER TABLE pbp.play_wide ADD PRIMARY KEY (play_uid);
CREATE INDEX play_wide_league_season_kind_idx ON pbp.play_wide (league, season, play_kind);
CREATE INDEX play_wide_kicker_idx             ON pbp.play_wide (kicker_athlete_id) WHERE kicker_athlete_id IS NOT NULL;
CREATE INDEX play_wide_returner_idx           ON pbp.play_wide (returner_athlete_id) WHERE returner_athlete_id IS NOT NULL;
CREATE INDEX play_wide_game_idx               ON pbp.play_wide (game_id);
CREATE INDEX play_wide_kicking_team_idx       ON pbp.play_wide (league, kicking_team_id, season);
CREATE INDEX play_wide_fg_idx                 ON pbp.play_wide (league, fg_dist_bucket) WHERE play_kind = 'field_goal';

-- ---------------------------------------------------------------- scrimmage_wide
-- The mirror of play_wide. Same league-carrying joins; see the note above.
DROP TABLE IF EXISTS pbp.scrimmage_wide;
CREATE TABLE pbp.scrimmage_wide AS
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

    -- ---- derived ----
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
    EXTRACT(MONTH FROM g.kickoff_utc)::smallint AS game_month,
    -- Constant true for the NFL; see the note in play_wide.
    CASE WHEN p.league = 'nfl' THEN true
         ELSE (ots.ncaa_division = 'FBS' AND dts.ncaa_division = 'FBS') END AS both_top_division
FROM pbp.scrimmage_play p
LEFT JOIN pbp.fact_game       g   ON g.game_id = p.game_id AND g.league = p.league
LEFT JOIN pbp.dim_venue       v   ON v.venue_id = p.venue_id
LEFT JOIN pbp.dim_team        ot  ON ot.team_id = p.offense_team_id AND ot.league = p.league
LEFT JOIN pbp.dim_team        dt  ON dt.team_id = p.defense_team_id AND dt.league = p.league
LEFT JOIN pbp.dim_team_season ots ON ots.team_id = p.offense_team_id AND ots.season = p.season AND ots.league = p.league
LEFT JOIN pbp.dim_team_season dts ON dts.team_id = p.defense_team_id AND dts.season = p.season AND dts.league = p.league
LEFT JOIN pbp.dim_athlete     pa  ON pa.athlete_id = p.passer_athlete_id
LEFT JOIN pbp.dim_athlete     ra  ON ra.athlete_id = p.rusher_athlete_id
LEFT JOIN pbp.dim_athlete     wa  ON wa.athlete_id = p.receiver_athlete_id
LEFT JOIN pbp.dim_athlete     ta  ON ta.athlete_id = p.tackler_athlete_id;

ALTER TABLE pbp.scrimmage_wide ADD PRIMARY KEY (play_uid);
CREATE INDEX scrimmage_wide_league_season_kind_idx ON pbp.scrimmage_wide (league, season, play_kind);
CREATE INDEX scrimmage_wide_passer_idx     ON pbp.scrimmage_wide (passer_athlete_id)   WHERE passer_athlete_id IS NOT NULL;
CREATE INDEX scrimmage_wide_rusher_idx     ON pbp.scrimmage_wide (rusher_athlete_id)   WHERE rusher_athlete_id IS NOT NULL;
CREATE INDEX scrimmage_wide_receiver_idx   ON pbp.scrimmage_wide (receiver_athlete_id) WHERE receiver_athlete_id IS NOT NULL;
CREATE INDEX scrimmage_wide_tackler_idx    ON pbp.scrimmage_wide (tackler_athlete_id)  WHERE tackler_athlete_id IS NOT NULL;
CREATE INDEX scrimmage_wide_game_idx       ON pbp.scrimmage_wide (game_id);
CREATE INDEX scrimmage_wide_offense_idx    ON pbp.scrimmage_wide (league, offense_team_id, season);
CREATE INDEX scrimmage_wide_defense_idx    ON pbp.scrimmage_wide (league, defense_team_id, season);
CREATE INDEX scrimmage_wide_drive_idx      ON pbp.scrimmage_wide (drive_id);

-- ---------------------------------------------------------------- season_status
--
-- One row per league-season, so a consumer can tell a season that is still being played from
-- one that is finished without hardcoding a year. The rule is deliberately self-maintaining:
-- a season is in progress while its most recent game is recent. Nothing has to be unset in
-- January -- roughly a month after the last bowl, 2026 stops being in progress on its own,
-- and the offseason correctly reports no season in progress at all.
--
-- Built from play_wide to match build_snapshot.py, which builds it from `play`. That means
-- it counts SPECIAL TEAMS plays, not all plays -- the `plays` column is a kick count. It is
-- kept that way rather than "improved" so the two stay comparable.
DROP TABLE IF EXISTS pbp.season_status;
CREATE TABLE pbp.season_status AS
SELECT league, season,
       count(DISTINCT game_id)                                      AS games,
       count(*)                                                     AS plays,
       max(week) FILTER (WHERE season_type = 'regular')             AS last_regular_week,
       max(kickoff_utc)                                             AS last_kickoff,
       max(kickoff_utc) > now() - INTERVAL '30 days'                AS is_in_progress
FROM pbp.play_wide
GROUP BY league, season;

ALTER TABLE pbp.season_status ADD PRIMARY KEY (league, season);

ANALYZE pbp.play_wide;
ANALYZE pbp.scrimmage_wide;
ANALYZE pbp.season_status;

\echo '=== wide tables built'
SELECT 'play_wide' AS t, league, count(*) FROM pbp.play_wide GROUP BY 1,2
UNION ALL SELECT 'scrimmage_wide', league, count(*) FROM pbp.scrimmage_wide GROUP BY 1,2
ORDER BY 1,2;
