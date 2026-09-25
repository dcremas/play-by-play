-- Phase 4 load, step 1 of 2: all-text staging tables for the athlete artifacts.
--
-- This step used to live outside the repository, which is why sql/reparse.sql warns that
-- a TRUNCATE-and-reinsert would "wipe the Phase 4 enrichment ... populated from
-- play_athlete_wide.csv by a step that is not in this directory". It is in this directory
-- now, so the athlete link is reproducible from the same command list as everything else.
--
--   .venv/bin/python scripts/build_dims.py athlete
--   psql -d pbp -f sql/load_athletes_1_stage.sql
--   psql -d pbp -c "\copy pbp.stg_dim_athlete FROM 'data/out/dim_athlete.csv' WITH (FORMAT csv, HEADER true)"
--   psql -d pbp -c "\copy pbp.stg_play_athlete FROM 'data/out/play_athlete.csv' WITH (FORMAT csv, HEADER true)"
--   psql -d pbp -c "\copy pbp.stg_play_athlete_wide FROM 'data/out/play_athlete_wide.csv' WITH (FORMAT csv, HEADER true)"
--   psql -d pbp -f sql/load_athletes_2_apply.sql
--
-- All-text for the same reason as load_1_stage.sql: the CSVs render a missing integer as an
-- empty field, which a direct \copy into bigint rejects. NULLIF + numeric absorbs it.

DROP TABLE IF EXISTS pbp.stg_dim_athlete;
CREATE TABLE pbp.stg_dim_athlete (
  athlete_id text, known_name text, full_name text, position text, jersey text,
  text_name text, text_name_confidence text, primary_role text, primary_team_id text,
  first_season text, last_season text, st_plays text, scrimmage_plays text
);

DROP TABLE IF EXISTS pbp.stg_play_athlete;
CREATE TABLE pbp.stg_play_athlete (
  play_uid text, role text, athlete_id text, ordinal text
);

DROP TABLE IF EXISTS pbp.stg_play_athlete_wide;
CREATE TABLE pbp.stg_play_athlete_wide (
  play_uid text, kicker_athlete_id text, returner_athlete_id text, tackler_athlete_id text
);
