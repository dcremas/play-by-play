-- In-season loader: replace ONE season in pbp.scrimmage_play, pbp.scrimmage_athlete and
-- pbp.drive, leaving every other season alone.
--
-- The incremental counterpart to load_scrimmage_2_insert.sql + load_bridge_drive_2_insert.sql,
-- exactly as sql/load_3_season.sql is to sql/load_2_insert.sql. Those TRUNCATE, which is
-- right for a backfill and catastrophic for a weekly run of 30 new games. Game-context
-- enrichment happens in the SAME transaction here so it cannot be forgotten.
--
-- Usage:  psql -d cfb -v season=2026 -f sql/load_scrimmage_3_season.sql
-- Staging is built by load_scrimmage_1_stage.sql and load_bridge_drive_1_stage.sql plus the
-- three \copy commands; rows for any other season in staging are ignored, not loaded.

\set ON_ERROR_STOP on

SELECT set_config('pbp.season', :'season', false);

BEGIN;

DO $$
DECLARE n bigint; s smallint := current_setting('pbp.season')::smallint;
BEGIN
  SELECT count(*) INTO n FROM pbp.stg_scrimmage
   WHERE NULLIF(season,'')::numeric::smallint = s;
  IF n = 0 THEN
    RAISE EXCEPTION 'staging holds no scrimmage rows for season % -- refusing to delete it', s;
  END IF;
  RAISE NOTICE 'staging: % scrimmage rows for season %', n, s;
END $$;

-- The bridge has no season of its own; it is scoped through the fact it points at. Delete it
-- BEFORE the fact, while the join still resolves.
DELETE FROM pbp.scrimmage_athlete b
 USING pbp.scrimmage_play p
 WHERE p.play_uid = b.play_uid AND p.season = :season;

DELETE FROM pbp.scrimmage_play WHERE season = :season;
DELETE FROM pbp.drive          WHERE season = :season;

INSERT INTO pbp.scrimmage_play (
  play_uid, source, game_id, season, week, season_type, play_kind, play_type_espn,
  drive_id, drive_number, period, clock_secs_period, wallclock_utc, down, distance,
  yards_to_goal, offense_team_id, defense_team_id, is_home_offense, score_diff_offense,
  yards_gained, end_down, end_distance, end_yards_to_goal, end_team_id, first_down_gained,
  is_complete, is_touchdown, is_turnover, is_penalty, is_scoring_play, points_scored,
  passer_athlete_id, rusher_athlete_id, receiver_athlete_id, tackler_athlete_id, play_text)
SELECT
  play_uid, source,
  NULLIF(game_id,'')::numeric::bigint,
  NULLIF(season,'')::numeric::smallint,
  NULLIF(week,'')::numeric::smallint,
  NULLIF(season_type,''), NULLIF(play_kind,''), NULLIF(play_type_espn,''),
  NULLIF(drive_id,''),
  NULLIF(drive_number,'')::numeric::smallint,
  NULLIF(period,'')::numeric::smallint,
  NULLIF(clock_secs_period,'')::numeric::integer,
  NULLIF(wallclock_utc,'')::timestamptz,
  NULLIF(down,'')::numeric::smallint,
  NULLIF(distance,'')::numeric::smallint,
  NULLIF(yards_to_goal,'')::numeric::smallint,
  NULLIF(offense_team_id,'')::numeric::integer,
  NULLIF(defense_team_id,'')::numeric::integer,
  NULLIF(is_home_offense,'')::boolean,
  NULLIF(score_diff_offense,'')::numeric::smallint,
  NULLIF(yards_gained,'')::numeric::smallint,
  NULLIF(end_down,'')::numeric::smallint,
  NULLIF(end_distance,'')::numeric::smallint,
  NULLIF(end_yards_to_goal,'')::numeric::smallint,
  NULLIF(end_team_id,'')::numeric::integer,
  NULLIF(first_down_gained,'')::boolean,
  NULLIF(is_complete,'')::boolean,
  NULLIF(is_touchdown,'')::boolean,
  NULLIF(is_turnover,'')::boolean,
  NULLIF(is_penalty,'')::boolean,
  NULLIF(is_scoring_play,'')::boolean,
  NULLIF(points_scored,'')::numeric::smallint,
  NULLIF(passer_athlete_id,'')::numeric::bigint,
  NULLIF(rusher_athlete_id,'')::numeric::bigint,
  NULLIF(receiver_athlete_id,'')::numeric::bigint,
  NULLIF(tackler_athlete_id,'')::numeric::bigint,
  NULLIF(play_text,'')
FROM pbp.stg_scrimmage
WHERE NULLIF(season,'')::numeric::smallint = :season;

INSERT INTO pbp.scrimmage_athlete (play_uid, role, athlete_id, ordinal)
SELECT DISTINCT ON (b.play_uid, b.role, b.athlete_id::bigint)
       b.play_uid, b.role, b.athlete_id::bigint,
       NULLIF(b.ordinal,'')::numeric::smallint
FROM pbp.stg_scrimmage_athlete b
JOIN pbp.scrimmage_play p ON p.play_uid = b.play_uid
WHERE p.season = :season
ORDER BY b.play_uid, b.role, b.athlete_id::bigint, NULLIF(b.ordinal,'')::numeric::smallint;

INSERT INTO pbp.drive (
  drive_uid, drive_id, game_id, season, week, season_type, drive_number,
  offense_team_id, defense_team_id, result, display_result, description, is_score,
  offensive_plays, plays_total, plays_scrimmage, yards, time_elapsed_secs,
  start_period, start_clock_secs, start_yards_to_goal, start_text,
  end_period, end_clock_secs, end_yards_to_goal, end_text)
SELECT
  drive_uid, NULLIF(drive_id,''),
  NULLIF(game_id,'')::numeric::bigint,
  NULLIF(season,'')::numeric::smallint,
  NULLIF(week,'')::numeric::smallint,
  NULLIF(season_type,''),
  NULLIF(drive_number,'')::numeric::smallint,
  NULLIF(offense_team_id,'')::numeric::integer,
  NULLIF(defense_team_id,'')::numeric::integer,
  NULLIF(result,''), NULLIF(display_result,''), NULLIF(description,''),
  NULLIF(is_score,'')::boolean,
  NULLIF(offensive_plays,'')::numeric::smallint,
  NULLIF(plays_total,'')::numeric::smallint,
  NULLIF(plays_scrimmage,'')::numeric::smallint,
  NULLIF(yards,'')::numeric::smallint,
  NULLIF(time_elapsed_secs,'')::numeric::integer,
  NULLIF(start_period,'')::numeric::smallint,
  NULLIF(start_clock_secs,'')::numeric::integer,
  NULLIF(start_yards_to_goal,'')::numeric::smallint,
  NULLIF(start_text,''),
  NULLIF(end_period,'')::numeric::smallint,
  NULLIF(end_clock_secs,'')::numeric::integer,
  NULLIF(end_yards_to_goal,'')::numeric::smallint,
  NULLIF(end_text,'')
FROM pbp.stg_drive
WHERE NULLIF(season,'')::numeric::smallint = :season;

UPDATE pbp.scrimmage_play p
   SET venue_id        = g.venue_id,
       neutral_site    = g.neutral_site,
       conference_game = g.conference_game
  FROM pbp.fact_game g
 WHERE g.game_id = p.game_id AND p.season = :season;

DROP TABLE pbp.stg_scrimmage;
DROP TABLE pbp.stg_scrimmage_athlete;
DROP TABLE pbp.stg_drive;
COMMIT;

ANALYZE pbp.scrimmage_play;
ANALYZE pbp.scrimmage_athlete;
ANALYZE pbp.drive;

\echo '=== loaded season, scrimmage by play kind'
SELECT play_kind, count(*) AS plays, count(DISTINCT game_id) AS games,
       count(venue_id) AS with_venue
FROM pbp.scrimmage_play WHERE season = :season
GROUP BY play_kind ORDER BY plays DESC;
\echo '=== drives and bridge rows for the season'
SELECT (SELECT count(*) FROM pbp.drive WHERE season = :season) AS drives,
       (SELECT count(*) FROM pbp.scrimmage_athlete b JOIN pbp.scrimmage_play p
          ON p.play_uid = b.play_uid WHERE p.season = :season) AS bridge_rows;
