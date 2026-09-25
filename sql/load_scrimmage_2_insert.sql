-- Step 2 of 2. Casts staging text into the typed fact table and drops staging.
--
-- TRUNCATE clears venue_id / neutral_site / conference_game, which are NOT in this column
-- list -- they come from sql/enrich_game_context.sql. Run that afterwards or every join to
-- dim_venue silently returns nothing, exactly as documented for the special-teams loader.

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

TRUNCATE pbp.scrimmage_play;
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
FROM pbp.stg_scrimmage;

DROP TABLE pbp.stg_scrimmage;
ANALYZE pbp.scrimmage_play;
