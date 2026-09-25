-- Step 2 of 2. Casts staging text into the typed fact table and drops staging.

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

TRUNCATE pbp.special_teams_play;
INSERT INTO pbp.special_teams_play (
  play_uid, source, game_id, season, week, season_type, play_kind, period,
  clock_secs_period, wallclock_utc, down, distance, yards_to_goal, kicking_team_id,
  receiving_team_id, is_home_kicking, score_diff_kicking, fg_distance_yds, fg_made,
  punt_gross_yds, punt_net_yds, kickoff_yds, return_yds, returned, touchback, onside,
  fair_catch, downed, out_of_bounds, kick_blocked, returned_for_td, converted,
  two_point_type, miss_reason, negated_by_penalty, kicker_name, returner_name,
  blocker_name, play_text, parse_confidence)
SELECT
  play_uid, source,
  NULLIF(game_id,'')::numeric::bigint,
  NULLIF(season,'')::numeric::smallint,
  NULLIF(week,'')::numeric::smallint,
  NULLIF(season_type,''), NULLIF(play_kind,''),
  NULLIF(period,'')::numeric::smallint,
  NULLIF(clock_secs_period,'')::numeric::integer,
  NULLIF(wallclock_utc,'')::timestamptz,
  NULLIF(down,'')::numeric::smallint,
  NULLIF(distance,'')::numeric::smallint,
  NULLIF(yards_to_goal,'')::numeric::smallint,
  NULLIF(kicking_team_id,'')::numeric::integer,
  NULLIF(receiving_team_id,'')::numeric::integer,
  NULLIF(is_home_kicking,'')::boolean,
  NULLIF(score_diff_kicking,'')::numeric::smallint,
  NULLIF(fg_distance_yds,'')::numeric::smallint,
  NULLIF(fg_made,'')::boolean,
  NULLIF(punt_gross_yds,'')::numeric::smallint,
  NULLIF(punt_net_yds,'')::numeric::smallint,
  NULLIF(kickoff_yds,'')::numeric::smallint,
  NULLIF(return_yds,'')::numeric::smallint,
  NULLIF(returned,'')::boolean,
  NULLIF(touchback,'')::boolean,
  NULLIF(onside,'')::boolean,
  NULLIF(fair_catch,'')::boolean,
  NULLIF(downed,'')::boolean,
  NULLIF(out_of_bounds,'')::boolean,
  NULLIF(kick_blocked,'')::boolean,
  NULLIF(returned_for_td,'')::boolean,
  NULLIF(converted,'')::boolean,
  NULLIF(two_point_type,''), NULLIF(miss_reason,''),
  NULLIF(negated_by_penalty,'')::boolean,
  NULLIF(kicker_name,''), NULLIF(returner_name,''), NULLIF(blocker_name,''),
  NULLIF(play_text,''), NULLIF(parse_confidence,'')
FROM pbp.stg_plays;

DROP TABLE pbp.stg_plays;
ANALYZE pbp.special_teams_play;
