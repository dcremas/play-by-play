-- Step 1 of 2. Creates the all-text staging table.
-- Then:  \copy pbp.stg_plays FROM 'data/out/st_plays.csv' WITH (FORMAT csv, HEADER true)
-- Then:  psql -f sql/load_2_insert.sql
-- Load via an all-text staging table: a feed that renders integers as "4.0" would be
-- rejected by a direct \copy into smallint. Casting through numeric absorbs both forms.
DROP TABLE IF EXISTS pbp.stg_plays;
CREATE TABLE pbp.stg_plays (
  play_uid text, source text, game_id text, season text, week text, season_type text,
  play_kind text, period text, clock_secs_period text, wallclock_utc text, down text,
  distance text, yards_to_goal text, kicking_team_id text, receiving_team_id text,
  is_home_kicking text, score_diff_kicking text, fg_distance_yds text, fg_made text,
  punt_gross_yds text, punt_net_yds text, kickoff_yds text, return_yds text, returned text,
  touchback text, onside text, fair_catch text, downed text, out_of_bounds text,
  kick_blocked text, returned_for_td text, converted text, two_point_type text,
  miss_reason text, negated_by_penalty text, kicker_name text, returner_name text,
  blocker_name text, play_text text, parse_confidence text
);

