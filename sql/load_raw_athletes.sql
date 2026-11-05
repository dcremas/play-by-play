-- Load the PER-LEAGUE athlete dimensions into raw.dim_athlete_league, where dbt reads them.
--
--     .venv/bin/python scripts/build_dims.py athlete --leagues cfb   # -> dim_athlete_cfb.csv
--     .venv/bin/python scripts/build_dims.py athlete --leagues nfl   # -> dim_athlete_nfl.csv
--     psql -d pbp -f sql/load_raw_athletes.sql
--
-- sql/split_leagues.sql COPYs these two files itself, mid-transform. dbt cannot read a file,
-- so the load moves out to here and the transform reads a table: extract and load in this
-- repo's scripts, transform in dbt/. The modal-vote logic that builds the files stays in
-- build_dims.py for the reason split_leagues.sql gives.
--
-- Its own schema, `raw`, rather than pbp.*: pbp is the eleven-table system of record that
-- README.md's data dictionary describes and the mirror copies, and this is neither.
--
-- Server-side COPY, not \copy -- see the note in sql/load_league.sql.

\set ON_ERROR_STOP on
\if :{?root}
\else
  \set root '/opt/pbp-etl'
\endif

\set cfb_csv :root '/data/out/dim_athlete_cfb.csv'
\set nfl_csv :root '/data/out/dim_athlete_nfl.csv'

CREATE SCHEMA IF NOT EXISTS raw;

BEGIN;

CREATE TEMP TABLE stg_athlete (
  athlete_id text, known_name text, full_name text, position text, jersey text,
  text_name text, text_name_confidence text, primary_role text, primary_team_id text,
  first_season text, last_season text, st_plays text, scrimmage_plays text,
  leagues text, nfl_plays text, date_of_birth text, debut_year text,
  height_in text, weight_lb text
) ON COMMIT DROP;

-- Created once and then emptied, never dropped: dbt's staging views select from it, and a
-- DROP fails on them. A column change here therefore needs a manual migration.
CREATE TABLE IF NOT EXISTS raw.dim_athlete_league (
  league text NOT NULL, LIKE stg_athlete,
  loaded_at timestamptz NOT NULL DEFAULT now()
);
TRUNCATE raw.dim_athlete_league;

COPY stg_athlete FROM :'cfb_csv' WITH (FORMAT csv, HEADER true);
INSERT INTO raw.dim_athlete_league SELECT 'cfb', s.* FROM stg_athlete s;
TRUNCATE stg_athlete;
COPY stg_athlete FROM :'nfl_csv' WITH (FORMAT csv, HEADER true);
INSERT INTO raw.dim_athlete_league SELECT 'nfl', s.* FROM stg_athlete s;

-- An empty league here would build a serving schema with no athletes and no error.
DO $$
DECLARE n_cfb bigint; n_nfl bigint;
BEGIN
    SELECT count(*) FILTER (WHERE league = 'cfb'), count(*) FILTER (WHERE league = 'nfl')
      INTO n_cfb, n_nfl FROM raw.dim_athlete_league;
    IF n_cfb = 0 OR n_nfl = 0 THEN
        RAISE EXCEPTION 'raw.dim_athlete_league is missing a league (cfb %, nfl %)', n_cfb, n_nfl;
    END IF;
    RAISE NOTICE 'loaded athletes: cfb %, nfl %', n_cfb, n_nfl;
END $$;

COMMIT;
