-- Step 1 of 2. Creates the all-text staging table for the scrimmage fact.
-- Then:  \copy pbp.stg_scrimmage FROM 'data/out/scrimmage_plays.csv' WITH (FORMAT csv, HEADER true)
-- Then:  psql -f sql/load_scrimmage_2_insert.sql
--
-- Same reason for the all-text hop as sql/load_1_stage.sql: a feed that renders an integer
-- as "4.0" is rejected by a direct \copy into smallint, and casting through numeric absorbs
-- both forms. Column order must match build_scrimmage.COLUMNS exactly.
DROP TABLE IF EXISTS pbp.stg_scrimmage;
CREATE TABLE pbp.stg_scrimmage (
  play_uid text, source text, game_id text, season text, week text, season_type text,
  play_kind text, play_type_espn text, drive_id text, drive_number text, period text,
  clock_secs_period text, wallclock_utc text, down text, distance text, yards_to_goal text,
  offense_team_id text, defense_team_id text, is_home_offense text, score_diff_offense text,
  yards_gained text, end_down text, end_distance text, end_yards_to_goal text,
  end_team_id text, first_down_gained text, is_complete text, is_touchdown text,
  is_turnover text, is_penalty text, is_scoring_play text, points_scored text,
  passer_athlete_id text, rusher_athlete_id text, receiver_athlete_id text,
  tackler_athlete_id text, play_text text
);
