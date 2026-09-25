-- Stage-2 load, step 2 of 2: cast staging into the typed tables and drop staging.

-- ---------------------------------------------------------------------------------------
-- SUPERSEDED 2026-09-11, and it REFUSES TO RUN once a second league is loaded.
--
-- This script is single-league by construction: it TRUNCATEs (or deletes without a league
-- predicate), which was exactly right when the warehouse held one corpus and is destructive
-- now that it holds two -- running it would delete the NFL to make room for college, or the
-- reverse, and report success.
--
-- Use sql/load_league.sql instead:
--     psql -d pbp -v league=nfl -f sql/load_league.sql               a whole league
--     psql -d pbp -v league=nfl -v season=2026 -f sql/load_league.sql  one season of it
--
-- Kept rather than deleted because the comments in it explain decisions the replacement
-- inherited. The guard below is what makes keeping it safe -- but a RAISE only stops psql
-- when ON_ERROR_STOP is set, and several of these files never set it. Without the line
-- below the exception is printed and the very next statement TRUNCATEs anyway, which is
-- not a hypothetical: it is how this guard was found to be insufficient.
\set ON_ERROR_STOP on
DO $guard$
BEGIN
  IF (SELECT count(DISTINCT league) FROM pbp.special_teams_play) > 1
     OR (SELECT count(DISTINCT league) FROM pbp.scrimmage_play) > 1 THEN
    RAISE EXCEPTION
      'This is a single-league loader and the warehouse holds more than one league. '
      'It would delete the other one. Use sql/load_league.sql -v league=<cfb|nfl>.';
  END IF;
END
$guard$;
-- ---------------------------------------------------------------------------------------

TRUNCATE pbp.scrimmage_athlete;
INSERT INTO pbp.scrimmage_athlete (play_uid, role, athlete_id, ordinal)
SELECT play_uid, role,
       NULLIF(athlete_id,'')::numeric::bigint,
       NULLIF(ordinal,'')::numeric::smallint
FROM pbp.stg_scrimmage_athlete;

TRUNCATE pbp.drive;
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
FROM pbp.stg_drive;

DROP TABLE pbp.stg_scrimmage_athlete;
DROP TABLE pbp.stg_drive;
ANALYZE pbp.scrimmage_athlete;
ANALYZE pbp.drive;
