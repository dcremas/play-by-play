-- Load the shared athlete dimension and BOTH leagues' athlete bridges.
--
--     .venv/bin/python scripts/build_dims.py athlete --leagues cfb nfl
--     psql -d pbp -f sql/load_athletes_league.sql
--
-- The two-league replacement for load_athletes_1_stage.sql + load_athletes_2_apply.sql.
-- Those two are single-league and TRUNCATE pbp.play_athlete, which after the NFL load would
-- delete 238,194 NFL bridge rows to make room for the college ones.
--
-- dim_athlete is NOT scoped by league and is deliberately replaced WHOLESALE. It is one row
-- per person across both corpora -- ESPN athlete ids are a single id space, verified at
-- 720/720 name agreement on a sampled overlap -- so scoping it per league would be
-- incoherent: Patrick Mahomes is not two people, and 2,490 of the 6,037 NFL athletes are
-- already in the college corpus under the same id. build_dims.py --leagues cfb nfl always
-- writes the complete union, so a wholesale swap cannot strand anybody.
--
-- Uses server-side COPY rather than \copy: psql's \copy does not interpolate :'variables'
-- and silently loads ZERO rows when handed one. See the note in sql/load_league.sql.

\set ON_ERROR_STOP on
\if :{?root}
\else
  \set root '/Users/dustincremascoli/projects/pbp'
\endif

SELECT r || '/data/out/dim_athlete.csv'          AS dim_csv,
       r || '/data/out/play_athlete.csv'         AS pa_cfb,
       r || '/data/out/play_athlete_nfl.csv'     AS pa_nfl,
       r || '/data/out/play_athlete_wide.csv'    AS w_cfb,
       r || '/data/out/play_athlete_wide_nfl.csv' AS w_nfl
FROM (SELECT :'root' AS r) q \gset

DROP TABLE IF EXISTS pbp.stg_dim_athlete;
CREATE TABLE pbp.stg_dim_athlete (
  athlete_id text, known_name text, full_name text, position text, jersey text,
  text_name text, text_name_confidence text, primary_role text, primary_team_id text,
  first_season text, last_season text, st_plays text, scrimmage_plays text,
  leagues text, nfl_plays text, date_of_birth text, debut_year text,
  height_in text, weight_lb text
);
DROP TABLE IF EXISTS pbp.stg_play_athlete;
CREATE TABLE pbp.stg_play_athlete (
  play_uid text, role text, athlete_id text, ordinal text);
DROP TABLE IF EXISTS pbp.stg_play_athlete_wide;
CREATE TABLE pbp.stg_play_athlete_wide (
  play_uid text, kicker_athlete_id text, returner_athlete_id text, tackler_athlete_id text);

COPY pbp.stg_dim_athlete       FROM :'dim_csv' WITH (FORMAT csv, HEADER true);
COPY pbp.stg_play_athlete      FROM :'pa_cfb'  WITH (FORMAT csv, HEADER true);
COPY pbp.stg_play_athlete      FROM :'pa_nfl'  WITH (FORMAT csv, HEADER true);
COPY pbp.stg_play_athlete_wide FROM :'w_cfb'   WITH (FORMAT csv, HEADER true);
COPY pbp.stg_play_athlete_wide FROM :'w_nfl'   WITH (FORMAT csv, HEADER true);

BEGIN;

-- Staging must not be EMPTY. Without this the script is a loaded gun: every guard below
-- passes vacuously on an empty set, the TRUNCATEs empty the dimension and the bridge, and
-- the apply puts nothing back -- all with exit code 0. Kept verbatim from the single-league
-- script, which is where that failure was actually found.
DO $$
DECLARE d bigint; b bigint; w bigint;
BEGIN
  SELECT count(*) INTO d FROM pbp.stg_dim_athlete;
  SELECT count(*) INTO b FROM pbp.stg_play_athlete;
  SELECT count(*) INTO w FROM pbp.stg_play_athlete_wide;
  IF d = 0 OR b = 0 OR w = 0 THEN
    RAISE EXCEPTION 'staging is empty (dim_athlete=%, play_athlete=%, wide=%) -- '
                    'the COPY steps did not run; refusing to replace the dimension', d, b, w;
  END IF;
  RAISE NOTICE 'staging: % athletes, % bridge rows, % wide rows', d, b, w;
END $$;

-- Refuse to run against a staging set built from a different fetch. Spans both leagues now,
-- because pbp.special_teams_play does.
DO $$
DECLARE orphans bigint;
BEGIN
  SELECT count(*) INTO orphans
  FROM pbp.stg_play_athlete_wide w
  WHERE NOT EXISTS (SELECT 1 FROM pbp.special_teams_play p WHERE p.play_uid = w.play_uid);
  IF orphans > 0 THEN
    RAISE EXCEPTION 'staging names % plays that are not in the fact table -- refusing to load',
                    orphans;
  END IF;
END $$;

TRUNCATE pbp.play_athlete;
INSERT INTO pbp.play_athlete (play_uid, role, athlete_id, ordinal)
SELECT DISTINCT ON (play_uid, role, athlete_id::bigint)
       play_uid, role, athlete_id::bigint, NULLIF(ordinal,'')::numeric::smallint
FROM pbp.stg_play_athlete
ORDER BY play_uid, role, athlete_id::bigint, NULLIF(ordinal,'')::numeric::smallint;

TRUNCATE pbp.dim_athlete;
INSERT INTO pbp.dim_athlete (athlete_id, known_name, full_name, position, jersey,
                             text_name, text_name_confidence, primary_role,
                             primary_team_id, first_season, last_season,
                             st_plays, scrimmage_plays, leagues, nfl_plays,
                             date_of_birth, debut_year, height_in, weight_lb)
SELECT athlete_id::bigint, NULLIF(known_name,''), NULLIF(full_name,''),
       NULLIF(position,''), NULLIF(jersey,''), NULLIF(text_name,''),
       NULLIF(text_name_confidence,'')::numeric, NULLIF(primary_role,''),
       NULLIF(primary_team_id,'')::numeric::integer,
       NULLIF(first_season,'')::numeric::smallint,
       NULLIF(last_season,'')::numeric::smallint,
       NULLIF(st_plays,'')::numeric::integer,
       NULLIF(scrimmage_plays,'')::numeric::integer,
       NULLIF(leagues,''), NULLIF(nfl_plays,'')::numeric::integer,
       -- The NFL athlete records carry a birth date; the college ones do not. Parsed
       -- leniently because the feed renders it both as a date and as a full timestamp.
       NULLIF(left(date_of_birth, 10),'')::date,
       NULLIF(debut_year,'')::numeric::smallint,
       NULLIF(height_in,'')::numeric::smallint,
       NULLIF(weight_lb,'')::numeric::smallint
FROM pbp.stg_dim_athlete;

-- Clear first, then apply: the staging set is the complete truth from the participants
-- corpus, so a play absent from it has no athlete identity and must not keep a stale id.
UPDATE pbp.special_teams_play
   SET kicker_athlete_id = NULL, returner_athlete_id = NULL, tackler_athlete_id = NULL;
UPDATE pbp.special_teams_play p SET
  kicker_athlete_id   = NULLIF(w.kicker_athlete_id,'')::numeric::bigint,
  returner_athlete_id = NULLIF(w.returner_athlete_id,'')::numeric::bigint,
  tackler_athlete_id  = NULLIF(w.tackler_athlete_id,'')::numeric::bigint
FROM pbp.stg_play_athlete_wide w
WHERE w.play_uid = p.play_uid;

DROP TABLE pbp.stg_dim_athlete;
DROP TABLE pbp.stg_play_athlete;
DROP TABLE pbp.stg_play_athlete_wide;
COMMIT;

ANALYZE pbp.dim_athlete;
ANALYZE pbp.play_athlete;

\echo '=== athletes by corpus (cfb+nfl is one person who played in both)'
SELECT leagues, count(*) AS athletes FROM pbp.dim_athlete GROUP BY 1 ORDER BY 2 DESC;

\echo '=== kicker_athlete_id coverage by league and play_kind'
SELECT league, play_kind, count(*) AS plays, count(kicker_athlete_id) AS linked,
       round(100.0 * count(kicker_athlete_id) / count(*), 2) AS pct
FROM pbp.special_teams_play GROUP BY 1,2 ORDER BY 1, 3 DESC;
