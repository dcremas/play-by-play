-- Re-derive the parser-owned columns from the stored play_text, in place.
--
-- PLAN.md §6 promised this: "play_text is retained on every row forever. When the parser
-- improves, everything is re-derivable without re-fetching." This is that path.
--
-- Deliberately an UPDATE, not the TRUNCATE + INSERT of load_2_insert.sql. That load path
-- would wipe the Phase 4 enrichment (kicker_athlete_id, returner_athlete_id,
-- tackler_athlete_id, venue_id, conference_game, neutral_site), which is populated from
-- play_athlete_wide.csv by a step that is not in this directory. Only the 25 columns the
-- loader actually derives are touched; the remaining ids, situation and play_text
-- are left alone.
--
-- kicking_team_id / receiving_team_id / is_home_kicking joined that list on 2026-08-30.
-- They are not parser output, but they ARE derived -- emit_pat picks the conversion's
-- team off the scoreboard -- and the unconditional swap it replaced had put ~94% of all
-- 58,535 PAT and two-point rows on the opposing team. Leaving them out of this UPDATE
-- would load a corrected CSV and change nothing.
--
--   .venv/bin/python scripts/build_table.py         -- regenerate data/out/st_plays.csv
--   psql -d cfb -f sql/load_1_stage.sql
--   psql -d cfb -c "\copy pbp.stg_plays FROM 'data/out/st_plays.csv' WITH (FORMAT csv, HEADER true)"
--   psql -d cfb -f sql/reparse.sql

BEGIN;

-- Reversible by construction: the pre-update values are kept until they are not wanted.
DROP TABLE IF EXISTS pbp.special_teams_play_prereparse;
CREATE TABLE pbp.special_teams_play_prereparse AS
SELECT play_uid, kicking_team_id, receiving_team_id, is_home_kicking,
       fg_distance_yds, fg_made, punt_gross_yds, punt_net_yds, kickoff_yds,
       return_yds, returned, touchback, onside, fair_catch, downed, out_of_bounds,
       kick_blocked, returned_for_td, converted, two_point_type, miss_reason,
       negated_by_penalty, kicker_name, returner_name, blocker_name, parse_confidence
FROM pbp.special_teams_play;

-- Every row must be present on both sides; a mismatch means the CSV was built from a
-- different fetch and this is the wrong operation.
DO $$
DECLARE missing bigint;
BEGIN
  SELECT count(*) INTO missing
  FROM pbp.special_teams_play p
  WHERE NOT EXISTS (SELECT 1 FROM pbp.stg_plays s WHERE s.play_uid = p.play_uid);
  IF missing > 0 THEN
    RAISE EXCEPTION 'staging is missing % existing plays -- refusing to reparse', missing;
  END IF;
END $$;

UPDATE pbp.special_teams_play p SET
  kicking_team_id    = NULLIF(s.kicking_team_id,'')::numeric::integer,
  receiving_team_id  = NULLIF(s.receiving_team_id,'')::numeric::integer,
  is_home_kicking    = NULLIF(s.is_home_kicking,'')::boolean,
  fg_distance_yds    = NULLIF(s.fg_distance_yds,'')::numeric::smallint,
  fg_made            = NULLIF(s.fg_made,'')::boolean,
  punt_gross_yds     = NULLIF(s.punt_gross_yds,'')::numeric::smallint,
  punt_net_yds       = NULLIF(s.punt_net_yds,'')::numeric::smallint,
  kickoff_yds        = NULLIF(s.kickoff_yds,'')::numeric::smallint,
  return_yds         = NULLIF(s.return_yds,'')::numeric::smallint,
  returned           = NULLIF(s.returned,'')::boolean,
  touchback          = NULLIF(s.touchback,'')::boolean,
  onside             = NULLIF(s.onside,'')::boolean,
  fair_catch         = NULLIF(s.fair_catch,'')::boolean,
  downed             = NULLIF(s.downed,'')::boolean,
  out_of_bounds      = NULLIF(s.out_of_bounds,'')::boolean,
  kick_blocked       = NULLIF(s.kick_blocked,'')::boolean,
  returned_for_td    = NULLIF(s.returned_for_td,'')::boolean,
  converted          = NULLIF(s.converted,'')::boolean,
  two_point_type     = NULLIF(s.two_point_type,''),
  miss_reason        = NULLIF(s.miss_reason,''),
  negated_by_penalty = NULLIF(s.negated_by_penalty,'')::boolean,
  kicker_name        = NULLIF(s.kicker_name,''),
  returner_name      = NULLIF(s.returner_name,''),
  blocker_name       = NULLIF(s.blocker_name,''),
  parse_confidence   = NULLIF(s.parse_confidence,'')
FROM pbp.stg_plays s
WHERE s.play_uid = p.play_uid;

DROP TABLE pbp.stg_plays;
COMMIT;

\echo '=== punts and kickoffs with no outcome flag set, by season (was ~5% pre-2021)'
SELECT season,
       round(100.0 * avg((NOT (coalesce(touchback,false) OR coalesce(fair_catch,false)
             OR coalesce(downed,false) OR coalesce(out_of_bounds,false)
             OR coalesce(returned,false) OR coalesce(kick_blocked,false)))::int)
             FILTER (WHERE play_kind='punt'), 1) AS punt_pct_unclassified,
       round(100.0 * avg((NOT (coalesce(touchback,false) OR coalesce(onside,false)
             OR coalesce(out_of_bounds,false) OR coalesce(returned,false)
             OR coalesce(fair_catch,false) OR coalesce(downed,false)))::int)
             FILTER (WHERE play_kind='kickoff'), 1) AS ko_pct_unclassified
FROM pbp.special_teams_play GROUP BY season ORDER BY season;
