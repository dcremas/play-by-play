-- In-season loader: replace ONE season in pbp.special_teams_play, leave the rest alone.
--
-- sql/load_2_insert.sql is the loader for a backfill: it TRUNCATEs the whole fact table,
-- which is right when every row is being rebuilt and catastrophic when 30 new games are.
-- Its TRUNCATE also clears venue_id / neutral_site / conference_game, which its own column
-- list does not repopulate -- the quiet failure documented in sql/enrich_game_context.sql.
--
-- This file is the incremental counterpart. It deletes and re-inserts a single season and
-- then re-applies that season's game context in the SAME transaction, so the enrichment
-- can never be forgotten. Re-running it is idempotent.
--
-- Usage:  psql -d pbp -v season=2026 -f sql/load_3_season.sql
-- Staging is built exactly as for a backfill (sql/load_1_stage.sql + \copy); rows for any
-- other season sitting in staging are ignored rather than loaded.


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

\set ON_ERROR_STOP on

-- psql does not substitute :season inside a dollar-quoted body, so the guards below read
-- it back out of a session setting instead. Everything outside a DO block uses :season
-- directly, which does substitute.
SELECT set_config('pbp.season', :'season', false);

BEGIN;

-- Refuse to run against a staging set that has nothing for the target season, which is
-- what a mis-built CSV or a wrong -v season looks like.
DO $$
DECLARE n bigint; s smallint := current_setting('pbp.season')::smallint;
BEGIN
  SELECT count(*) INTO n FROM pbp.stg_plays
   WHERE NULLIF(season,'')::numeric::smallint = s;
  IF n = 0 THEN
    RAISE EXCEPTION 'staging holds no rows for season % -- refusing to delete it', s;
  END IF;
  RAISE NOTICE 'staging: % rows for season %', n, s;
END $$;

DELETE FROM pbp.special_teams_play WHERE season = :season;

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
FROM pbp.stg_plays
WHERE NULLIF(season,'')::numeric::smallint = :season;

-- The three columns load_2_insert.sql leaves NULL, restored for this season only. Doing it
-- here rather than in a separate script is the whole point: every venue join downstream
-- depends on it and nothing errors when it is missing.
UPDATE pbp.special_teams_play p
   SET venue_id        = g.venue_id,
       neutral_site    = g.neutral_site,
       conference_game = g.conference_game
  FROM pbp.fact_game g
 WHERE g.game_id = p.game_id AND p.season = :season;

DROP TABLE pbp.stg_plays;
COMMIT;

ANALYZE pbp.special_teams_play;

\echo '=== loaded season, by play kind'
SELECT play_kind, count(*) AS plays, count(DISTINCT game_id) AS games,
       count(venue_id) AS with_venue
FROM pbp.special_teams_play WHERE season = :season
GROUP BY play_kind ORDER BY plays DESC;
