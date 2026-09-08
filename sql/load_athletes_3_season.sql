-- In-season athlete link: rebuild the dimension globally, re-point ONE season's plays.
--
-- The counterpart to sql/load_athletes_2_apply.sql, which is right for a backfill and wrong
-- weekly. That script TRUNCATEs pbp.play_athlete and then clears and re-applies the three id
-- columns on every row of pbp.special_teams_play -- 313k rows rewritten to add thirty games,
-- three times over once the enrichment UPDATEs follow, which is exactly the bloat the
-- README's "reclaiming space after a reload" section exists to mop up.
--
-- The split here follows what each table actually is:
--
--   pbp.dim_athlete    a CAREER aggregate (first_season, last_season, st_plays, the modal
--                     known_name and primary_team_id). It cannot be scoped to a season --
--                     a 2026-only rebuild would give every returning kicker
--                     first_season = 2026. build_dims.py athlete --plays therefore derives
--                     it from the whole corpus, and it is replaced wholesale here. At 33k
--                     rows that is cheap.
--
--   pbp.play_athlete   keyed per play, so it scopes cleanly. Staging holds only the target
--   the three id      season (build_dims.py athlete --only-season), and only that season's
--   columns           rows are deleted, re-inserted and re-pointed.
--
-- Usage:  psql -d cfb -v season=2026 -f sql/load_athletes_3_season.sql
-- Staging is built exactly as for a backfill: sql/load_athletes_1_stage.sql + three \copy.

\set ON_ERROR_STOP on

-- psql does not substitute :season inside a dollar-quoted body, so the guards below read
-- it back out of a session setting instead. Everything outside a DO block uses :season
-- directly, which does substitute.
SELECT set_config('pbp.season', :'season', false);

BEGIN;

-- Staging must not be EMPTY. Without this the script is a loaded gun: every guard below
-- passes vacuously on an empty set (no orphans, no strays), TRUNCATE pbp.dim_athlete then
-- empties the dimension, the bridge DELETE removes the season's rows, and the apply puts
-- nothing back -- all with exit code 0. One mistyped \copy path is enough to trigger it,
-- and that is exactly how it was found.
DO $$
DECLARE d bigint; b bigint; w bigint;
BEGIN
  SELECT count(*) INTO d FROM pbp.stg_dim_athlete;
  SELECT count(*) INTO b FROM pbp.stg_play_athlete;
  SELECT count(*) INTO w FROM pbp.stg_play_athlete_wide;
  IF d = 0 OR b = 0 OR w = 0 THEN
    RAISE EXCEPTION 'staging is empty (dim_athlete=%, play_athlete=%, wide=%) -- '
                    'the \copy steps did not run; refusing to replace the dimension',
                    d, b, w;
  END IF;
  RAISE NOTICE 'staging: % athletes, % bridge rows, % wide rows', d, b, w;
END $$;

-- Same guard as load_athletes_2_apply.sql: staging naming a play the fact table does not
-- have means the two halves were built from different extracts.
DO $$
DECLARE orphans bigint;
BEGIN
  SELECT count(*) INTO orphans
  FROM pbp.stg_play_athlete_wide w
  WHERE NOT EXISTS (SELECT 1 FROM pbp.special_teams_play p WHERE p.play_uid = w.play_uid);
  IF orphans > 0 THEN
    RAISE EXCEPTION 'staging names % plays that are not in the fact table -- refusing to load', orphans;
  END IF;
END $$;

-- And one this file needs that the backfill does not: staging must be scoped to the season
-- being loaded, or the DELETE below would strip bridge rows the INSERT never puts back.
DO $$
DECLARE strays bigint; s smallint := current_setting('pbp.season')::smallint;
BEGIN
  SELECT count(*) INTO strays
  FROM pbp.stg_play_athlete_wide w
  JOIN pbp.special_teams_play p USING (play_uid)
  WHERE p.season <> s;
  IF strays > 0 THEN
    RAISE EXCEPTION 'staging holds % plays outside season % -- rebuild with --only-season %',
                    strays, s, s;
  END IF;
END $$;

-- The dimension: global, replaced wholesale.
TRUNCATE pbp.dim_athlete;
INSERT INTO pbp.dim_athlete (athlete_id, known_name, full_name, position, jersey,
                            text_name, text_name_confidence, primary_role,
                            primary_team_id, first_season, last_season,
                            st_plays, scrimmage_plays)
SELECT athlete_id::bigint,
       NULLIF(known_name,''),
       NULLIF(full_name,''),
       NULLIF(position,''),
       NULLIF(jersey,''),
       NULLIF(text_name,''),
       NULLIF(text_name_confidence,'')::numeric,
       NULLIF(primary_role,''),
       NULLIF(primary_team_id,'')::numeric::integer,
       NULLIF(first_season,'')::numeric::smallint,
       NULLIF(last_season,'')::numeric::smallint,
       NULLIF(st_plays,'')::numeric::integer,
       NULLIF(scrimmage_plays,'')::numeric::integer
FROM pbp.stg_dim_athlete;

-- The bridge: this season only.
DELETE FROM pbp.play_athlete b
 USING pbp.special_teams_play p
 WHERE p.play_uid = b.play_uid AND p.season = :season;

-- DISTINCT ON guards the (play_uid, role, athlete_id) primary key, as in the backfill: a
-- play listing the same athlete twice in one role would otherwise abort the load.
INSERT INTO pbp.play_athlete (play_uid, role, athlete_id, ordinal)
SELECT DISTINCT ON (play_uid, role, athlete_id::bigint)
       play_uid, role, athlete_id::bigint, NULLIF(ordinal,'')::numeric::smallint
FROM pbp.stg_play_athlete
ORDER BY play_uid, role, athlete_id::bigint, NULLIF(ordinal,'')::numeric::smallint;

-- Clear then apply, so the result does not depend on what ran before it -- but only over
-- this season's rows.
UPDATE pbp.special_teams_play
   SET kicker_athlete_id = NULL, returner_athlete_id = NULL, tackler_athlete_id = NULL
 WHERE season = :season;

UPDATE pbp.special_teams_play p SET
  kicker_athlete_id   = NULLIF(w.kicker_athlete_id,'')::numeric::bigint,
  returner_athlete_id = NULLIF(w.returner_athlete_id,'')::numeric::bigint,
  tackler_athlete_id  = NULLIF(w.tackler_athlete_id,'')::numeric::bigint
FROM pbp.stg_play_athlete_wide w
WHERE w.play_uid = p.play_uid AND p.season = :season;

DROP TABLE pbp.stg_dim_athlete;
DROP TABLE pbp.stg_play_athlete;
DROP TABLE pbp.stg_play_athlete_wide;
COMMIT;

\echo '=== kicker_athlete_id coverage for the loaded season'
SELECT play_kind, count(*) AS plays, count(kicker_athlete_id) AS linked,
       round(100.0 * count(kicker_athlete_id) / count(*), 2) AS pct
FROM pbp.special_teams_play WHERE season = :season
GROUP BY play_kind ORDER BY plays DESC;

\echo '=== the three kicking phases, every season (earlier seasons must not move)'
SELECT season, count(*) AS kicks, count(kicker_athlete_id) AS linked,
       round(100.0 * count(kicker_athlete_id) / count(*), 2) AS pct
FROM pbp.special_teams_play
WHERE play_kind IN ('punt','kickoff','field_goal')
GROUP BY season ORDER BY season;
