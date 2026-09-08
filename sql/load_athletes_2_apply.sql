-- Phase 4 load, step 2 of 2: replace the athlete dimension and bridge, then re-point the
-- three id columns on the fact table.

BEGIN;

-- Reversible by construction, like sql/reparse.sql. Keep until the new coverage is trusted.
DROP TABLE IF EXISTS st.special_teams_play_preathletefix;
CREATE TABLE st.special_teams_play_preathletefix AS
SELECT play_uid, kicker_athlete_id, returner_athlete_id, tackler_athlete_id
FROM st.special_teams_play;

-- Refuse to run against a staging set built from a different fetch.
DO $$
DECLARE orphans bigint;
BEGIN
  SELECT count(*) INTO orphans
  FROM st.stg_play_athlete_wide w
  WHERE NOT EXISTS (SELECT 1 FROM st.special_teams_play p WHERE p.play_uid = w.play_uid);
  IF orphans > 0 THEN
    RAISE EXCEPTION 'staging names % plays that are not in the fact table -- refusing to load', orphans;
  END IF;
END $$;

TRUNCATE st.play_athlete;
-- DISTINCT ON guards the (play_uid, role, athlete_id) primary key: a play that lists the
-- same athlete twice in one role would otherwise abort the load.
INSERT INTO st.play_athlete (play_uid, role, athlete_id, ordinal)
SELECT DISTINCT ON (play_uid, role, athlete_id::bigint)
       play_uid, role, athlete_id::bigint, NULLIF(ordinal,'')::numeric::smallint
FROM st.stg_play_athlete
ORDER BY play_uid, role, athlete_id::bigint, NULLIF(ordinal,'')::numeric::smallint;

TRUNCATE st.dim_athlete;
INSERT INTO st.dim_athlete (athlete_id, known_name, full_name, position, jersey,
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
FROM st.stg_dim_athlete;

-- Clear first, then apply. The staging set is the complete truth from the participants
-- corpus, so a play absent from it has no athlete identity -- leaving a stale id behind
-- would make the load depend on whatever ran before it.
UPDATE st.special_teams_play
   SET kicker_athlete_id = NULL, returner_athlete_id = NULL, tackler_athlete_id = NULL;

UPDATE st.special_teams_play p SET
  kicker_athlete_id   = NULLIF(w.kicker_athlete_id,'')::numeric::bigint,
  returner_athlete_id = NULLIF(w.returner_athlete_id,'')::numeric::bigint,
  tackler_athlete_id  = NULLIF(w.tackler_athlete_id,'')::numeric::bigint
FROM st.stg_play_athlete_wide w
WHERE w.play_uid = p.play_uid;

DROP TABLE st.stg_dim_athlete;
DROP TABLE st.stg_play_athlete;
DROP TABLE st.stg_play_athlete_wide;
COMMIT;

\echo '=== kicker_athlete_id coverage by play_kind (conversions were 0% before this fix)'
SELECT play_kind,
       count(*) AS plays,
       count(kicker_athlete_id) AS linked,
       round(100.0 * count(kicker_athlete_id) / count(*), 2) AS pct
FROM st.special_teams_play GROUP BY play_kind ORDER BY plays DESC;

\echo '=== the three kicking phases by season (2025 was 58% before this fix)'
SELECT season,
       count(*) AS kicks,
       count(kicker_athlete_id) AS linked,
       round(100.0 * count(kicker_athlete_id) / count(*), 2) AS pct
FROM st.special_teams_play
WHERE play_kind IN ('punt','kickoff','field_goal')
GROUP BY season ORDER BY season;
