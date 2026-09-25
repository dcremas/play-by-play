-- Build ONE league's schema: cfb.* or nfl.*, with no `league` column anywhere.
--
--     psql -d pbp -v league=cfb -v schema=cfb_next -v root=$PWD -f sql/split_leagues.sql
--
-- BUILD INTO A STAGING SCHEMA, THEN SWAP. `schema` is the TARGET and is normally
-- `<league>_next`, not the live name. This file opens with DROP SCHEMA ... CASCADE on its
-- target, and pointing that at a live corpus is a 1m39s outage on the EC2 box for a
-- public endpoint -- see sql/swap_schema.sql, which moves the finished schema into place
-- in under a millisecond. The full order is:
--
--     split_leagues.sql   -v schema=cfb_next        build
--     setup_role_pbp.sql  -v schemas=cfb_next       grant
--     comment_tables.sql  -v schema=cfb_next        comment
--     swap_schema.sql     -v live=cfb -v stage=cfb_next
--
-- Grants and comments live on table oids and survive the rename, so the corpus arrives
-- already readable and already documented. scripts/sync_ec2.py and scripts/update_season.py
-- both drive exactly that sequence; running this file straight at a live schema name still
-- works and is what a first install does, when there is nothing to keep available.
--
-- WHY THIS EXISTS
-- ---------------
-- College football and the NFL are answered separately, the way ESPN segregates them, and
-- no question crosses the two. Everything the combined `pbp` schema does to keep both in one
-- place is therefore pure cost: a `league` column on seven tables, a `league` predicate on
-- every team join, and fourteen team ids that mean one thing in one corpus and another in
-- the other -- team 2 is Auburn AND the Buffalo Bills. A join that forgets `league` matches
-- both and doubles the answer, silently.
--
-- After this, `league` does not exist. `search_path` selects the corpus, every table name is
-- identical in both, and a cross-league query is not something you can write by accident.
--
-- THIS IS A DERIVED SERVING LAYER, NOT THE SYSTEM OF RECORD. `pbp.*` remains the eleven
-- tables the loaders build and README.md's data dictionary describes; these two schemas are
-- projections of it, exactly as sql/wide_tables.sql was. Drop and rebuild rather than
-- repairing. It REPLACES wide_tables.sql -- pbp.play_wide, pbp.scrimmage_wide and
-- pbp.season_status are dropped at the end, because their per-league twins supersede them.
--
-- THE ONE TABLE THAT CANNOT BE FILTERED
-- -------------------------------------
-- Every table here is `SELECT ... WHERE league = :'league'` except dim_athlete, and that
-- exception is the entire reason this file is not a set of views.
--
-- pbp.dim_athlete is CAREER grain across BOTH corpora. Its aggregates are sums over both:
--
--     Patrick Mahomes  primary_team_id = 12  -> Kansas City Chiefs in the NFL,
--                                               and ARIZONA WILDCATS in college.
--     Josh Allen       primary_team_id =  2  -> Buffalo Bills, and AUBURN TIGERS.
--
-- Mahomes played at Texas Tech and Allen at Wyoming. A filtered view would state both wrong
-- answers confidently, because team ids collide and the id was chosen from the NFL half of a
-- combined career. scrimmage_plays, first_season, last_season and primary_role are wrong the
-- same way. So the per-league dimension is REBUILT, by the same Python that builds the
-- combined one:
--
--     python scripts/build_dims.py athlete --leagues cfb   # -> dim_athlete_cfb.csv
--     python scripts/build_dims.py athlete --leagues nfl   # -> dim_athlete_nfl.csv
--
-- Reusing that builder rather than porting its modal-vote logic to SQL is deliberate: the
-- role canonicalisation (patScorer -> kicker), the name vote's two-sightings-and-60% rule
-- and the deterministic tie-break are each documented in build_dims.py as having been got
-- wrong once already.
--
-- The counts confirm the split is exactly what it should be: 64,840 college + 6,495 NFL
-- against 68,389 combined. The 2,946 difference is precisely the cfb+nfl population, now
-- correctly one row in each corpus rather than one row spanning both.
--
-- WHAT IS GIVEN UP: the 2,946 cross-league careers as a single row. That was the question
-- this warehouse was keyed to answer and it is now unanswerable without going back to pbp.*.
-- That is the decision, not an oversight.

\set ON_ERROR_STOP on
\timing off

\echo ''
\echo '=== building schema :schema from league :league'

DROP SCHEMA IF EXISTS :schema CASCADE;
CREATE SCHEMA :schema;

-- ---------------------------------------------------------------- dimensions

-- Teams and conferences: the `league` predicate is applied once, here, and then the column
-- is gone. Inside this schema team 2 has exactly one meaning.
-- SELECT * then DROP COLUMN, rather than naming columns. Naming them means this file
-- silently stops carrying a column the day someone adds one upstream, and an enumerated
-- list written from memory is wrong in a way psql reports only at run time -- which is how
-- the first draft of this file came to reference dim_team.mascot, a column that has never
-- existed.
CREATE TABLE :schema.dim_team AS SELECT * FROM pbp.dim_team WHERE league = :'league';
ALTER TABLE :schema.dim_team DROP COLUMN league;
ALTER TABLE :schema.dim_team ADD PRIMARY KEY (team_id);

CREATE TABLE :schema.dim_conference AS SELECT * FROM pbp.dim_conference WHERE league = :'league';
ALTER TABLE :schema.dim_conference DROP COLUMN league;
ALTER TABLE :schema.dim_conference ADD PRIMARY KEY (conference_id);

CREATE TABLE :schema.dim_team_season AS SELECT * FROM pbp.dim_team_season WHERE league = :'league';
ALTER TABLE :schema.dim_team_season DROP COLUMN league;
ALTER TABLE :schema.dim_team_season ADD PRIMARY KEY (team_id, season);

-- Venues have no league column -- ESPN's venue ids are one id space because a bowl game at
-- AT&T Stadium is that same building, and 35 venues host both corpora. Scoped here to the
-- venues this league actually played in, so `find_venue` in the college schema does not
-- return an NFL-only stadium.
CREATE TABLE :schema.dim_venue AS
SELECT v.* FROM pbp.dim_venue v
WHERE EXISTS (SELECT 1 FROM pbp.fact_game g
              WHERE g.venue_id = v.venue_id AND g.league = :'league');
ALTER TABLE :schema.dim_venue ADD PRIMARY KEY (venue_id);

-- ---------------------------------------------------------------- dim_athlete (REBUILT)
--
-- Staged as text and cast, mirroring sql/load_athletes_league.sql, because the CSV carries
-- empty strings for absent numerics. `leagues` and `nfl_plays` are read off the file and
-- then DISCARDED: inside a single-league schema they are respectively a constant and a
-- duplicate of scrimmage/st counts, and keeping them would invite exactly the cross-league
-- reasoning this split removes.
-- Staged in `public` under a FIXED name, not in :schema, for one reason: psql does not
-- interpolate its variables inside a dollar-quoted block, so a guard written as
-- `format('... %I.stg_dim_athlete', :'schema')` inside DO $$ ... $$ fails to parse. Naming
-- the table literally is what lets the guard below be a real RAISE EXCEPTION rather than a
-- printed warning that psql sails straight past. Dropped at the end of each league's run.
DROP TABLE IF EXISTS public.stg_dim_athlete;
CREATE TABLE public.stg_dim_athlete (
  athlete_id text, known_name text, full_name text, position text, jersey text,
  text_name text, text_name_confidence text, primary_role text, primary_team_id text,
  first_season text, last_season text, st_plays text, scrimmage_plays text,
  leagues text, nfl_plays text, date_of_birth text, debut_year text,
  height_in text, weight_lb text
);

\set athlete_csv :root '/data/out/dim_athlete_' :league '.csv'
COPY public.stg_dim_athlete FROM :'athlete_csv' WITH (FORMAT csv, HEADER true);

-- Every guard in this repo's loaders exists because its absence let a silent emptying
-- through once. An empty staging table here would produce an athlete-less schema in which
-- every player question returns nothing and no step reports an error.
DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM public.stg_dim_athlete;
    IF n = 0 THEN
        RAISE EXCEPTION 'stg_dim_athlete is EMPTY -- the dim_athlete CSV was not found or not readable by the SERVER. Server-side COPY reads paths on the database host, not the client.';
    END IF;
    RAISE NOTICE 'staged % athletes', n;
END $$;

CREATE TABLE :schema.dim_athlete AS
SELECT NULLIF(athlete_id,'')::numeric::bigint            AS athlete_id,
       NULLIF(known_name,'')                             AS known_name,
       NULLIF(full_name,'')                              AS full_name,
       NULLIF(position,'')                               AS position,
       NULLIF(jersey,'')                                 AS jersey,
       NULLIF(text_name,'')                              AS text_name,
       NULLIF(text_name_confidence,'')::numeric          AS text_name_confidence,
       NULLIF(primary_role,'')                           AS primary_role,
       NULLIF(primary_team_id,'')::numeric::integer      AS primary_team_id,
       NULLIF(first_season,'')::numeric::smallint        AS first_season,
       NULLIF(last_season,'')::numeric::smallint         AS last_season,
       NULLIF(st_plays,'')::numeric::integer             AS st_plays,
       NULLIF(scrimmage_plays,'')::numeric::integer      AS scrimmage_plays,
       NULLIF(date_of_birth,'')::date                    AS date_of_birth,
       NULLIF(debut_year,'')::numeric::smallint          AS debut_year,
       NULLIF(height_in,'')::numeric::smallint           AS height_in,
       NULLIF(weight_lb,'')::numeric::smallint           AS weight_lb
FROM public.stg_dim_athlete;
ALTER TABLE :schema.dim_athlete ADD PRIMARY KEY (athlete_id);
DROP TABLE public.stg_dim_athlete;

-- ---------------------------------------------------------------- facts

CREATE TABLE :schema.fact_game AS SELECT * FROM pbp.fact_game WHERE league = :'league';
ALTER TABLE :schema.fact_game DROP COLUMN league;
ALTER TABLE :schema.fact_game ADD PRIMARY KEY (game_id);

CREATE TABLE :schema.drive AS SELECT * FROM pbp.drive WHERE league = :'league';
ALTER TABLE :schema.drive DROP COLUMN league;
ALTER TABLE :schema.drive ADD PRIMARY KEY (drive_uid);

CREATE TABLE :schema.special_teams_play AS
SELECT * FROM pbp.special_teams_play WHERE league = :'league';
ALTER TABLE :schema.special_teams_play DROP COLUMN league;
ALTER TABLE :schema.special_teams_play ADD PRIMARY KEY (play_uid);

CREATE TABLE :schema.scrimmage_play AS
SELECT * FROM pbp.scrimmage_play WHERE league = :'league';
ALTER TABLE :schema.scrimmage_play DROP COLUMN league;
ALTER TABLE :schema.scrimmage_play ADD PRIMARY KEY (play_uid);

-- The bridges carry no league of their own; they take it from the fact through play_uid.
-- Joining rather than filtering is also what keeps the 73 stranded orphans of
-- README.md Known limits section 14 OUT of these schemas -- a bridge row whose play_uid is
-- no longer in the fact cannot be reached by this join, which is the correct outcome.
CREATE TABLE :schema.play_athlete AS
SELECT b.* FROM pbp.play_athlete b
JOIN :schema.special_teams_play p ON p.play_uid = b.play_uid;

CREATE TABLE :schema.scrimmage_athlete AS
SELECT b.* FROM pbp.scrimmage_athlete b
JOIN :schema.scrimmage_play p ON p.play_uid = b.play_uid;

CREATE INDEX play_athlete_athlete_idx      ON :schema.play_athlete (athlete_id);
CREATE INDEX play_athlete_play_idx         ON :schema.play_athlete (play_uid);
CREATE INDEX scrimmage_athlete_athlete_idx ON :schema.scrimmage_athlete (athlete_id);
CREATE INDEX scrimmage_athlete_play_idx    ON :schema.scrimmage_athlete (play_uid);

-- ---------------------------------------------------------------- serving layer
--
-- The same projection sql/wide_tables.sql built, minus every `AND x.league = p.league` --
-- there is only one league in here now, so the join that used to be the most likely way to
-- get a wrong answer from this warehouse cannot be written wrong.

CREATE TABLE :schema.play_wide AS
SELECT
    p.play_uid, p.source, p.game_id, p.season, p.week, p.season_type, p.play_kind,
    p.period, p.clock_secs_period, p.wallclock_utc, p.down, p.distance, p.yards_to_goal,
    p.kicking_team_id, p.receiving_team_id, p.is_home_kicking, p.score_diff_kicking,
    p.fg_distance_yds, p.fg_made, p.punt_gross_yds, p.punt_net_yds, p.kickoff_yds,
    p.return_yds, p.returned, p.touchback, p.onside, p.fair_catch, p.downed,
    p.out_of_bounds, p.kick_blocked, p.returned_for_td, p.converted, p.two_point_type,
    p.miss_reason, p.negated_by_penalty,
    p.kicker_athlete_id, p.returner_athlete_id, p.tackler_athlete_id,
    p.kicker_name, p.returner_name, p.blocker_name, p.snapper_name, p.holder_name,
    ka.known_name           AS kicker_known_name,
    ka.text_name_confidence AS kicker_name_confidence,
    ka.position             AS kicker_position,
    g.kickoff_utc, g.attendance, p.neutral_site, p.conference_game,
    p.venue_id, v.venue_name, v.city AS venue_city, v.state AS venue_state,
    v.country AS venue_country, v.surface, v.indoor AS venue_indoor,
    kt.display_name AS kicking_team, rt.display_name AS receiving_team,
    kts.conference_name AS kicking_conference, kts.ncaa_division AS kicking_ncaa_division,
    kts.nfl_division   AS kicking_nfl_division,
    rts.conference_name AS receiving_conference, rts.ncaa_division AS receiving_ncaa_division,
    rts.nfl_division   AS receiving_nfl_division,
    p.play_text, p.parse_confidence,
    CASE WHEN p.period IS NULL OR p.clock_secs_period IS NULL THEN NULL
         WHEN p.period > 4 THEN 0
         ELSE (4 - p.period) * 900 + p.clock_secs_period END AS game_secs_remaining,
    (p.period >= 4 AND abs(p.score_diff_kicking) <= 8
        AND (p.period > 4 OR p.clock_secs_period <= 300)) AS is_clutch,
    CASE WHEN p.fg_distance_yds IS NULL THEN NULL
         WHEN p.fg_distance_yds < 20 THEN 15
         WHEN p.fg_distance_yds >= 60 THEN 60
         ELSE (p.fg_distance_yds / 5) * 5 END AS fg_dist_bucket,
    EXTRACT(MONTH FROM g.kickoff_utc)::smallint AS game_month,
    -- Constant true for the NFL, which has no second division. Folded at plan time now that
    -- the league is fixed for the whole table.
    CASE WHEN :'league' = 'nfl' THEN true
         ELSE (kts.ncaa_division = 'FBS' AND rts.ncaa_division = 'FBS') END AS both_top_division
FROM :schema.special_teams_play p
LEFT JOIN :schema.fact_game       g   ON g.game_id = p.game_id
LEFT JOIN :schema.dim_venue       v   ON v.venue_id = p.venue_id
LEFT JOIN :schema.dim_team        kt  ON kt.team_id = p.kicking_team_id
LEFT JOIN :schema.dim_team        rt  ON rt.team_id = p.receiving_team_id
LEFT JOIN :schema.dim_team_season kts ON kts.team_id = p.kicking_team_id   AND kts.season = p.season
LEFT JOIN :schema.dim_team_season rts ON rts.team_id = p.receiving_team_id AND rts.season = p.season
LEFT JOIN :schema.dim_athlete     ka  ON ka.athlete_id = p.kicker_athlete_id;

ALTER TABLE :schema.play_wide ADD PRIMARY KEY (play_uid);
CREATE INDEX play_wide_season_kind_idx  ON :schema.play_wide (season, play_kind);
CREATE INDEX play_wide_kicker_idx       ON :schema.play_wide (kicker_athlete_id)   WHERE kicker_athlete_id IS NOT NULL;
CREATE INDEX play_wide_returner_idx     ON :schema.play_wide (returner_athlete_id) WHERE returner_athlete_id IS NOT NULL;
CREATE INDEX play_wide_game_idx         ON :schema.play_wide (game_id);
CREATE INDEX play_wide_kicking_team_idx ON :schema.play_wide (kicking_team_id, season);
CREATE INDEX play_wide_fg_idx           ON :schema.play_wide (fg_dist_bucket) WHERE play_kind = 'field_goal';

CREATE TABLE :schema.scrimmage_wide AS
SELECT
    p.play_uid, p.source, p.game_id, p.season, p.week, p.season_type,
    p.play_kind, p.play_type_espn, p.drive_id, p.drive_number,
    p.period, p.clock_secs_period, p.wallclock_utc, p.down, p.distance, p.yards_to_goal,
    p.offense_team_id, p.defense_team_id, p.is_home_offense, p.score_diff_offense,
    p.yards_gained, p.end_down, p.end_distance, p.end_yards_to_goal, p.end_team_id,
    p.first_down_gained, p.is_complete, p.is_touchdown, p.is_turnover, p.is_penalty,
    p.is_scoring_play, p.points_scored,
    p.passer_athlete_id, p.rusher_athlete_id, p.receiver_athlete_id, p.tackler_athlete_id,
    pa.known_name AS passer_name,   pa.position AS passer_position,
    ra.known_name AS rusher_name,   ra.position AS rusher_position,
    wa.known_name AS receiver_name, wa.position AS receiver_position,
    ta.known_name AS tackler_name,  ta.position AS tackler_position,
    g.kickoff_utc, g.attendance, p.neutral_site, p.conference_game,
    p.venue_id, v.venue_name, v.city AS venue_city, v.state AS venue_state,
    v.country AS venue_country, v.surface, v.indoor AS venue_indoor,
    ot.display_name AS offense_team, dt.display_name AS defense_team,
    ots.conference_name AS offense_conference, ots.ncaa_division AS offense_ncaa_division,
    ots.nfl_division   AS offense_nfl_division,
    dts.conference_name AS defense_conference, dts.ncaa_division AS defense_ncaa_division,
    dts.nfl_division   AS defense_nfl_division,
    p.play_text,
    CASE WHEN p.period IS NULL OR p.clock_secs_period IS NULL THEN NULL
         WHEN p.period > 4 THEN 0
         ELSE (4 - p.period) * 900 + p.clock_secs_period END AS game_secs_remaining,
    (p.period >= 4 AND abs(p.score_diff_offense) <= 8
        AND (p.period > 4 OR p.clock_secs_period <= 300)) AS is_clutch,
    CASE WHEN p.down IS NULL OR p.distance IS NULL THEN NULL
         WHEN p.distance <= 3 THEN 'short'
         WHEN p.distance <= 7 THEN 'medium'
         ELSE 'long' END AS distance_bucket,
    CASE WHEN p.yards_to_goal IS NULL THEN NULL
         WHEN p.yards_to_goal <= 20 THEN 'red zone'
         WHEN p.yards_to_goal <= 50 THEN 'opponent half'
         ELSE 'own half' END AS field_zone,
    EXTRACT(MONTH FROM g.kickoff_utc)::smallint AS game_month,
    CASE WHEN :'league' = 'nfl' THEN true
         ELSE (ots.ncaa_division = 'FBS' AND dts.ncaa_division = 'FBS') END AS both_top_division
FROM :schema.scrimmage_play p
LEFT JOIN :schema.fact_game       g   ON g.game_id = p.game_id
LEFT JOIN :schema.dim_venue       v   ON v.venue_id = p.venue_id
LEFT JOIN :schema.dim_team        ot  ON ot.team_id = p.offense_team_id
LEFT JOIN :schema.dim_team        dt  ON dt.team_id = p.defense_team_id
LEFT JOIN :schema.dim_team_season ots ON ots.team_id = p.offense_team_id AND ots.season = p.season
LEFT JOIN :schema.dim_team_season dts ON dts.team_id = p.defense_team_id AND dts.season = p.season
LEFT JOIN :schema.dim_athlete     pa  ON pa.athlete_id = p.passer_athlete_id
LEFT JOIN :schema.dim_athlete     ra  ON ra.athlete_id = p.rusher_athlete_id
LEFT JOIN :schema.dim_athlete     wa  ON wa.athlete_id = p.receiver_athlete_id
LEFT JOIN :schema.dim_athlete     ta  ON ta.athlete_id = p.tackler_athlete_id;

ALTER TABLE :schema.scrimmage_wide ADD PRIMARY KEY (play_uid);
CREATE INDEX scrimmage_wide_season_kind_idx ON :schema.scrimmage_wide (season, play_kind);
CREATE INDEX scrimmage_wide_passer_idx      ON :schema.scrimmage_wide (passer_athlete_id)   WHERE passer_athlete_id IS NOT NULL;
CREATE INDEX scrimmage_wide_rusher_idx      ON :schema.scrimmage_wide (rusher_athlete_id)   WHERE rusher_athlete_id IS NOT NULL;
CREATE INDEX scrimmage_wide_receiver_idx    ON :schema.scrimmage_wide (receiver_athlete_id) WHERE receiver_athlete_id IS NOT NULL;
CREATE INDEX scrimmage_wide_tackler_idx     ON :schema.scrimmage_wide (tackler_athlete_id)  WHERE tackler_athlete_id IS NOT NULL;
CREATE INDEX scrimmage_wide_game_idx        ON :schema.scrimmage_wide (game_id);
CREATE INDEX scrimmage_wide_offense_idx     ON :schema.scrimmage_wide (offense_team_id, season);
CREATE INDEX scrimmage_wide_defense_idx     ON :schema.scrimmage_wide (defense_team_id, season);
CREATE INDEX scrimmage_wide_drive_idx       ON :schema.scrimmage_wide (drive_id);

-- Same self-maintaining rule and the same SPECIAL-TEAMS play count as before, kept that way
-- so the numbers stay comparable with build_snapshot.py's `season_status`.
CREATE TABLE :schema.season_status AS
SELECT season,
       count(DISTINCT game_id)                          AS games,
       count(*)                                         AS plays,
       max(week) FILTER (WHERE season_type = 'regular') AS last_regular_week,
       max(kickoff_utc)                                 AS last_kickoff,
       max(kickoff_utc) > now() - INTERVAL '30 days'    AS is_in_progress
FROM :schema.play_wide
GROUP BY season;
ALTER TABLE :schema.season_status ADD PRIMARY KEY (season);

ANALYZE :schema.play_wide;
ANALYZE :schema.scrimmage_wide;
ANALYZE :schema.season_status;
ANALYZE :schema.dim_athlete;
ANALYZE :schema.scrimmage_athlete;
ANALYZE :schema.play_athlete;

-- ---------------------------------------------------------------- retire the old layer
--
-- pbp.play_wide, pbp.scrimmage_wide and pbp.season_status were the COMBINED serving layer
-- sql/wide_tables.sql built, and the per-league twins above supersede them completely.
-- Dropped rather than left in place for two reasons, and the second is the real one:
--
--   1. They are ~2.4 GB on a box with single-digit gigabytes free.
--   2. pbp.play_wide is exactly the league-blind table this split exists to remove. Leaving
--      it reachable means the trap is still there for anyone who types its name -- and it
--      would go stale the moment a sync rebuilt the schemas without rebuilding it.
--
-- IF NOT EXISTS because the local warehouse never built them: wide_tables.sql only ever ran
-- against the mirror.
DROP TABLE IF EXISTS pbp.play_wide;
DROP TABLE IF EXISTS pbp.scrimmage_wide;
DROP TABLE IF EXISTS pbp.season_status;

\echo ''
\echo '=== :schema built'
SELECT 'dim_athlete' AS t, count(*) FROM :schema.dim_athlete
UNION ALL SELECT 'dim_team',           count(*) FROM :schema.dim_team
UNION ALL SELECT 'dim_team_season',    count(*) FROM :schema.dim_team_season
UNION ALL SELECT 'dim_conference',     count(*) FROM :schema.dim_conference
UNION ALL SELECT 'dim_venue',          count(*) FROM :schema.dim_venue
UNION ALL SELECT 'fact_game',          count(*) FROM :schema.fact_game
UNION ALL SELECT 'drive',              count(*) FROM :schema.drive
UNION ALL SELECT 'special_teams_play', count(*) FROM :schema.special_teams_play
UNION ALL SELECT 'scrimmage_play',     count(*) FROM :schema.scrimmage_play
UNION ALL SELECT 'play_athlete',       count(*) FROM :schema.play_athlete
UNION ALL SELECT 'scrimmage_athlete',  count(*) FROM :schema.scrimmage_athlete
UNION ALL SELECT 'play_wide',          count(*) FROM :schema.play_wide
UNION ALL SELECT 'scrimmage_wide',     count(*) FROM :schema.scrimmage_wide
UNION ALL SELECT 'season_status',      count(*) FROM :schema.season_status
ORDER BY 1;
