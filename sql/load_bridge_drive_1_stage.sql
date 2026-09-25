-- Stage-2 load, step 1 of 2: all-text staging for the scrimmage bridge and the drive table.
--
-- Both are staged in one file for the same reason sql/load_athletes_1_stage.sql stages three
-- tables at once: they are produced by one run of scripts/build_scrimmage.py and there is no
-- state in which you want one without the other.
--
--   .venv/bin/python scripts/build_scrimmage.py
--   psql -d pbp -f sql/load_bridge_drive_1_stage.sql
--   psql -d pbp -c "\copy pbp.stg_scrimmage_athlete FROM 'data/out/scrimmage_athlete.csv' WITH (FORMAT csv, HEADER true)"
--   psql -d pbp -c "\copy pbp.stg_drive FROM 'data/out/drives.csv' WITH (FORMAT csv, HEADER true)"
--   psql -d pbp -f sql/load_bridge_drive_2_insert.sql

DROP TABLE IF EXISTS pbp.stg_scrimmage_athlete;
CREATE TABLE pbp.stg_scrimmage_athlete (
  play_uid text, role text, athlete_id text, ordinal text
);

DROP TABLE IF EXISTS pbp.stg_drive;
CREATE TABLE pbp.stg_drive (
  drive_uid text, drive_id text, game_id text, season text, week text, season_type text,
  drive_number text, offense_team_id text, defense_team_id text, result text,
  display_result text, description text, is_score text, offensive_plays text,
  plays_total text, plays_scrimmage text, yards text, time_elapsed_secs text,
  start_period text, start_clock_secs text, start_yards_to_goal text, start_text text,
  end_period text, end_clock_secs text, end_yards_to_goal text, end_text text
);
