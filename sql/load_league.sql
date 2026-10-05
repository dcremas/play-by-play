-- Load ONE league's whole corpus: both facts, the bridge, drives, and the dimensions.
--
--     psql -d pbp -v league=nfl -f sql/load_league.sql
--     psql -d pbp -v league=cfb -f sql/load_league.sql
--
-- Replaces the eight single-league scripts for a two-league warehouse. Those scripts
-- TRUNCATE, which was correct when there was one corpus and is now the single most
-- destructive thing in this repository: running sql/load_2_insert.sql after the NFL is
-- loaded would delete all 90,819 NFL kicks to make room for the college ones. Everything
-- here is scoped -- DELETE ... WHERE league = :'league' -- so loading one corpus cannot
-- touch the other.
--
-- ONE EXCEPTION, and it is deliberate: pbp.dim_venue is NOT scoped, because venue ids are a
-- single id space across both feeds and a stadium that hosts both a bowl game and an NFL
-- team is one building with one id. It is UPSERTed rather than replaced, so whichever
-- league loads second adds its new venues and refreshes the shared ones instead of deleting
-- the other league's -- filling gaps, never blanking a field the other league supplied.
--
-- Run sql/enrich_game_context.sql afterwards: venue_id, neutral_site and conference_game on
-- the two facts are filled from fact_game AFTER the load, and are not in the column lists
-- below.

\set ON_ERROR_STOP on
\if :{?league}
\else
  \set league cfb
\endif
\if :{?root}
\else
  \set root '/Users/dustincremascoli/projects/pbp'
\endif
-- Optional. With `-v season=2026` this becomes the IN-SEASON loader: it reads the
-- season-scoped extracts and replaces only that season within the league, leaving the other
-- twelve untouched. Without it, the whole league is replaced. Same file either way, because
-- the two differ by one predicate and keeping them as two files is how the backfill loader
-- and the weekly loader drift apart.
\if :{?season}
\else
  \set season 0
\endif

\echo '=== loading league:' :league

-- Per-league file paths, computed in SQL and pulled back as psql variables. The college
-- extracts keep their historical bare names; every other league gets a '_<league>' suffix.
-- Built with \gset rather than \set concatenation, which does not survive an empty suffix.
-- NOTE: server-side COPY, not \copy, and that is not a style choice. psql's \copy has its
-- own parser and does NOT interpolate :'variables' -- it silently loads ZERO rows rather
-- than erroring, which is the worst possible failure for a loader. COPY is ordinary SQL, so
-- the paths below expand correctly. It needs an absolute path and a superuser (or
-- pg_read_server_files) connection; `root` defaults to this checkout.
SELECT r || '/data/out/st_plays'           || s || y || '.csv' AS st_csv,
       r || '/data/out/scrimmage_plays'    || s || y || '.csv' AS scrim_csv,
       r || '/data/out/scrimmage_athlete'  || s || y || '.csv' AS bridge_csv,
       r || '/data/out/drives'             || s || y || '.csv' AS drives_csv,
       r || '/data/out/dim_venue'          || s || '.csv' AS venue_csv,
       r || '/data/out/dim_team'           || s || '.csv' AS team_csv,
       r || '/data/out/dim_conference'     || s || '.csv' AS conf_csv,
       r || '/data/out/dim_team_season'    || s || '.csv' AS ts_csv,
       r || '/data/out/fact_game'          || s || '.csv' AS game_csv
FROM (SELECT CASE WHEN :'league' = 'cfb' THEN '' ELSE '_' || :'league' END AS s,
             CASE WHEN :season = 0 THEN '' ELSE '_' || :'season' END AS y,
             :'root' AS r) q \gset

-- The season predicate, reused by every DELETE below. Empty for a full-league load.
SELECT CASE WHEN :season = 0 THEN '' ELSE ' AND season = ' || :'season' END AS sp,
       -- The same predicate, table-qualified. The two enrichment UPDATEs join fact_game to
       -- a fact and BOTH carry `season`, so the bare form is ambiguous there.
       CASE WHEN :season = 0 THEN '' ELSE ' AND p.season = ' || :'season' END AS psp,
       -- The same predicate as a WHERE on a staging scan: staging may legitimately hold
       -- other seasons (the full extract read by a season-scoped run), and they are
       -- IGNORED rather than loaded. Without this the insert replays the whole league
       -- into a table that only deleted one season of it.
       -- Staging is all-text, so the comparison casts rather than relying on an
       -- implicit one that does not exist.
       CASE WHEN :season = 0 THEN 'TRUE'
            ELSE 'length(season) > 0 AND season::numeric::int = ' || :'season' END AS sw \gset

BEGIN;

-- ---------------------------------------------------------------- dimensions
-- The three team dimensions are always replaced whole for the league, even on a
-- season-scoped run: they are small (416 NFL team-seasons, 3,368 college), build_dims.py
-- always writes the entire window, and a season-scoped swap could not add a team that
-- joined mid-window anyway. fact_game IS season-scoped, because it is a fact.
-- dim_venue first: fact_game.venue_id references it. UPSERT, never DELETE -- see the header.
CREATE TEMP TABLE stg_venue (venue_id text, venue_name text, city text, state text,
                             zip text, country text, surface text, indoor text);
COPY stg_venue FROM :'venue_csv' WITH (FORMAT csv, HEADER true);

INSERT INTO pbp.dim_venue (venue_id, venue_name, city, state, zip, country, surface, indoor)
SELECT NULLIF(venue_id,'')::integer, NULLIF(venue_name,''), NULLIF(city,''),
       NULLIF(state,''), NULLIF(zip,''), NULLIF(country,''), NULLIF(surface,''),
       NULLIF(indoor,'')::boolean
FROM stg_venue
-- Never overwrite a known value with NULL, on ANY column. The two leagues describe a
-- shared venue from different payloads, and either may leave a field blank: the roof
-- comes from the scoreboard and only one league's may state it, and venue 3932 (SDCCU
-- Stadium) carries zip 92108 in the college summaries and none in the NFL's. A plain
-- `= EXCLUDED` made the answer depend on which league loaded last -- local and the
-- mirror disagreed on that zip on 2026-10-05, with every row count matching.
-- Two NON-null values that differ still go to the last writer; none do today.
ON CONFLICT (venue_id) DO UPDATE SET
  venue_name = COALESCE(EXCLUDED.venue_name, pbp.dim_venue.venue_name),
  city       = COALESCE(EXCLUDED.city,       pbp.dim_venue.city),
  state      = COALESCE(EXCLUDED.state,      pbp.dim_venue.state),
  zip        = COALESCE(EXCLUDED.zip,        pbp.dim_venue.zip),
  country    = COALESCE(EXCLUDED.country,    pbp.dim_venue.country),
  surface    = COALESCE(EXCLUDED.surface,    pbp.dim_venue.surface),
  indoor     = COALESCE(EXCLUDED.indoor,     pbp.dim_venue.indoor);

DELETE FROM pbp.fact_game       WHERE league = :'league' :sp ;
DELETE FROM pbp.dim_team_season WHERE league = :'league';
DELETE FROM pbp.dim_conference  WHERE league = :'league';
DELETE FROM pbp.dim_team        WHERE league = :'league';

COPY pbp.dim_team        (league, team_id, display_name) FROM :'team_csv' WITH (FORMAT csv, HEADER true);
COPY pbp.dim_conference  (league, conference_id, conference_name, short_name) FROM :'conf_csv' WITH (FORMAT csv, HEADER true);
COPY pbp.dim_team_season (league, team_id, season, conference_id, conference_name, ncaa_division, nfl_division) FROM :'ts_csv' WITH (FORMAT csv, HEADER true);

CREATE TEMP TABLE stg_game (league text, game_id text, season text, week text,
  season_type text, kickoff_utc text, home_team_id text, away_team_id text, venue_id text,
  attendance text, neutral_site text, conference_game text);
COPY stg_game FROM :'game_csv' WITH (FORMAT csv, HEADER true);
INSERT INTO pbp.fact_game (league, game_id, season, week, season_type, kickoff_utc,
                           home_team_id, away_team_id, venue_id, attendance, neutral_site,
                           conference_game)
SELECT league, NULLIF(game_id,'')::numeric::bigint, NULLIF(season,'')::numeric::smallint,
       NULLIF(week,'')::numeric::smallint,
       CASE WHEN season_type = '3' THEN 'postseason'
            WHEN season_type = '2' THEN 'regular' ELSE NULLIF(season_type,'') END,
       NULLIF(kickoff_utc,'')::timestamptz,
       NULLIF(home_team_id,'')::numeric::integer, NULLIF(away_team_id,'')::numeric::integer,
       NULLIF(venue_id,'')::numeric::integer, NULLIF(attendance,'')::numeric::integer,
       NULLIF(neutral_site,'')::boolean, NULLIF(conference_game,'')::boolean
FROM stg_game WHERE :sw ;

-- ---------------------------------------------------------------- special teams fact
CREATE TEMP TABLE stg_st (
  play_uid text, league text, source text, game_id text, season text, week text,
  season_type text, play_kind text, period text, clock_secs_period text, wallclock_utc text,
  down text, distance text, yards_to_goal text, kicking_team_id text, receiving_team_id text,
  is_home_kicking text, score_diff_kicking text, fg_distance_yds text, fg_made text,
  punt_gross_yds text, punt_net_yds text, kickoff_yds text, return_yds text, returned text,
  touchback text, onside text, fair_catch text, downed text, out_of_bounds text,
  kick_blocked text, returned_for_td text, converted text, two_point_type text,
  miss_reason text, negated_by_penalty text, kicker_name text, returner_name text,
  blocker_name text, snapper_name text, holder_name text,
  play_text text, parse_confidence text);
COPY stg_st FROM :'st_csv' WITH (FORMAT csv, HEADER true);

DELETE FROM pbp.special_teams_play WHERE league = :'league' :sp ;
INSERT INTO pbp.special_teams_play (
  play_uid, league, source, game_id, season, week, season_type, play_kind, period,
  clock_secs_period, wallclock_utc, down, distance, yards_to_goal, kicking_team_id,
  receiving_team_id, is_home_kicking, score_diff_kicking, fg_distance_yds, fg_made,
  punt_gross_yds, punt_net_yds, kickoff_yds, return_yds, returned, touchback, onside,
  fair_catch, downed, out_of_bounds, kick_blocked, returned_for_td, converted,
  two_point_type, miss_reason, negated_by_penalty, kicker_name, returner_name,
  blocker_name, snapper_name, holder_name, play_text, parse_confidence)
SELECT play_uid, league, source,
  NULLIF(game_id,'')::numeric::bigint, NULLIF(season,'')::numeric::smallint,
  NULLIF(week,'')::numeric::smallint, NULLIF(season_type,''), NULLIF(play_kind,''),
  NULLIF(period,'')::numeric::smallint, NULLIF(clock_secs_period,'')::numeric::integer,
  NULLIF(wallclock_utc,'')::timestamptz, NULLIF(down,'')::numeric::smallint,
  NULLIF(distance,'')::numeric::smallint, NULLIF(yards_to_goal,'')::numeric::smallint,
  NULLIF(kicking_team_id,'')::numeric::integer, NULLIF(receiving_team_id,'')::numeric::integer,
  NULLIF(is_home_kicking,'')::boolean, NULLIF(score_diff_kicking,'')::numeric::smallint,
  NULLIF(fg_distance_yds,'')::numeric::smallint, NULLIF(fg_made,'')::boolean,
  NULLIF(punt_gross_yds,'')::numeric::smallint, NULLIF(punt_net_yds,'')::numeric::smallint,
  NULLIF(kickoff_yds,'')::numeric::smallint, NULLIF(return_yds,'')::numeric::smallint,
  NULLIF(returned,'')::boolean, NULLIF(touchback,'')::boolean, NULLIF(onside,'')::boolean,
  NULLIF(fair_catch,'')::boolean, NULLIF(downed,'')::boolean,
  NULLIF(out_of_bounds,'')::boolean, NULLIF(kick_blocked,'')::boolean,
  NULLIF(returned_for_td,'')::boolean, NULLIF(converted,'')::boolean,
  NULLIF(two_point_type,''), NULLIF(miss_reason,''), NULLIF(negated_by_penalty,'')::boolean,
  NULLIF(kicker_name,''), NULLIF(returner_name,''), NULLIF(blocker_name,''),
  NULLIF(snapper_name,''), NULLIF(holder_name,''),
  NULLIF(play_text,''), NULLIF(parse_confidence,'')
FROM stg_st WHERE :sw ;

-- ---------------------------------------------------------------- scrimmage fact
CREATE TEMP TABLE stg_scrim (
  play_uid text, league text, source text, game_id text, season text, week text,
  season_type text, play_kind text, play_type_espn text, drive_id text, drive_number text,
  period text, clock_secs_period text, wallclock_utc text, down text, distance text,
  yards_to_goal text, offense_team_id text, defense_team_id text, is_home_offense text,
  score_diff_offense text, yards_gained text, end_down text, end_distance text,
  end_yards_to_goal text, end_team_id text, first_down_gained text, is_complete text,
  is_touchdown text, is_turnover text, is_penalty text, is_scoring_play text,
  points_scored text, passer_athlete_id text, rusher_athlete_id text,
  receiver_athlete_id text, tackler_athlete_id text, play_text text);
COPY stg_scrim FROM :'scrim_csv' WITH (FORMAT csv, HEADER true);

DELETE FROM pbp.scrimmage_play WHERE league = :'league' :sp ;
INSERT INTO pbp.scrimmage_play (
  play_uid, league, source, game_id, season, week, season_type, play_kind, play_type_espn,
  drive_id, drive_number, period, clock_secs_period, wallclock_utc, down, distance,
  yards_to_goal, offense_team_id, defense_team_id, is_home_offense, score_diff_offense,
  yards_gained, end_down, end_distance, end_yards_to_goal, end_team_id, first_down_gained,
  is_complete, is_touchdown, is_turnover, is_penalty, is_scoring_play, points_scored,
  passer_athlete_id, rusher_athlete_id, receiver_athlete_id, tackler_athlete_id, play_text)
SELECT play_uid, league, source,
  NULLIF(game_id,'')::numeric::bigint, NULLIF(season,'')::numeric::smallint,
  NULLIF(week,'')::numeric::smallint, NULLIF(season_type,''), NULLIF(play_kind,''),
  NULLIF(play_type_espn,''), NULLIF(drive_id,''),
  NULLIF(drive_number,'')::numeric::smallint, NULLIF(period,'')::numeric::smallint,
  NULLIF(clock_secs_period,'')::numeric::integer, NULLIF(wallclock_utc,'')::timestamptz,
  NULLIF(down,'')::numeric::smallint, NULLIF(distance,'')::numeric::smallint,
  NULLIF(yards_to_goal,'')::numeric::smallint, NULLIF(offense_team_id,'')::numeric::integer,
  NULLIF(defense_team_id,'')::numeric::integer, NULLIF(is_home_offense,'')::boolean,
  NULLIF(score_diff_offense,'')::numeric::smallint, NULLIF(yards_gained,'')::numeric::smallint,
  NULLIF(end_down,'')::numeric::smallint, NULLIF(end_distance,'')::numeric::smallint,
  NULLIF(end_yards_to_goal,'')::numeric::smallint, NULLIF(end_team_id,'')::numeric::integer,
  NULLIF(first_down_gained,'')::boolean, NULLIF(is_complete,'')::boolean,
  NULLIF(is_touchdown,'')::boolean, NULLIF(is_turnover,'')::boolean,
  NULLIF(is_penalty,'')::boolean, NULLIF(is_scoring_play,'')::boolean,
  NULLIF(points_scored,'')::numeric::smallint, NULLIF(passer_athlete_id,'')::numeric::bigint,
  NULLIF(rusher_athlete_id,'')::numeric::bigint, NULLIF(receiver_athlete_id,'')::numeric::bigint,
  NULLIF(tackler_athlete_id,'')::numeric::bigint, NULLIF(play_text,'')
FROM stg_scrim WHERE :sw ;

-- ---------------------------------------------------------------- bridge and drives
-- The bridge has no league column: it is keyed on play_uid, which is globally unique, so
-- its rows are scoped by joining the fact rather than by a column of their own.
DELETE FROM pbp.scrimmage_athlete sa
 USING pbp.scrimmage_play sp
 WHERE sa.play_uid = sp.play_uid AND sp.league = :'league' :sp ;
CREATE TEMP TABLE stg_bridge (play_uid text, role text, athlete_id text, ordinal text);
COPY stg_bridge FROM :'bridge_csv' WITH (FORMAT csv, HEADER true);
-- The bridge carries no season of its own, so it is scoped through the fact rows that
-- were just loaded. An EXISTS rather than a join: a play_uid appears many times here.
INSERT INTO pbp.scrimmage_athlete (play_uid, role, athlete_id, ordinal)
SELECT b.play_uid, b.role, NULLIF(b.athlete_id,'')::numeric::bigint,
       NULLIF(b.ordinal,'')::numeric::smallint
FROM stg_bridge b
WHERE EXISTS (SELECT 1 FROM pbp.scrimmage_play p WHERE p.play_uid = b.play_uid)
ON CONFLICT (play_uid, role, athlete_id) DO NOTHING;

DELETE FROM pbp.drive WHERE league = :'league' :sp ;
CREATE TEMP TABLE stg_drive (
  drive_uid text, league text, drive_id text, game_id text, season text, week text,
  season_type text, drive_number text, offense_team_id text, defense_team_id text,
  result text, display_result text, description text, is_score text, offensive_plays text,
  plays_total text, plays_scrimmage text, yards text, time_elapsed_secs text,
  start_period text, start_clock_secs text, start_yards_to_goal text, start_text text,
  end_period text, end_clock_secs text, end_yards_to_goal text, end_text text);
COPY stg_drive FROM :'drives_csv' WITH (FORMAT csv, HEADER true);
INSERT INTO pbp.drive (drive_uid, league, drive_id, game_id, season, week, season_type,
  drive_number, offense_team_id, defense_team_id, result, display_result, description,
  is_score, offensive_plays, plays_total, plays_scrimmage, yards, time_elapsed_secs,
  start_period, start_clock_secs, start_yards_to_goal, start_text, end_period,
  end_clock_secs, end_yards_to_goal, end_text)
SELECT drive_uid, league, NULLIF(drive_id,''), NULLIF(game_id,'')::numeric::bigint,
  NULLIF(season,'')::numeric::smallint, NULLIF(week,'')::numeric::smallint,
  NULLIF(season_type,''), NULLIF(drive_number,'')::numeric::smallint,
  NULLIF(offense_team_id,'')::numeric::integer, NULLIF(defense_team_id,'')::numeric::integer,
  NULLIF(result,''), NULLIF(display_result,''), NULLIF(description,''),
  NULLIF(is_score,'')::boolean, NULLIF(offensive_plays,'')::numeric::smallint,
  NULLIF(plays_total,'')::numeric::smallint, NULLIF(plays_scrimmage,'')::numeric::smallint,
  NULLIF(yards,'')::numeric::smallint, NULLIF(time_elapsed_secs,'')::numeric::integer,
  NULLIF(start_period,'')::numeric::smallint, NULLIF(start_clock_secs,'')::numeric::integer,
  NULLIF(start_yards_to_goal,'')::numeric::smallint, NULLIF(start_text,''),
  NULLIF(end_period,'')::numeric::smallint, NULLIF(end_clock_secs,'')::numeric::integer,
  NULLIF(end_yards_to_goal,'')::numeric::smallint, NULLIF(end_text,'')
FROM stg_drive WHERE :sw ;

-- Game context, in the SAME transaction as the load that cleared it. venue_id,
-- neutral_site and conference_game are not in either INSERT's column list, so a load that
-- did not do this would leave them NULL and every join to dim_venue would silently return
-- nothing. sql/enrich_game_context.sql is the standalone version of these two statements.
UPDATE pbp.special_teams_play p
   SET venue_id = g.venue_id, neutral_site = g.neutral_site,
       conference_game = g.conference_game
  FROM pbp.fact_game g
 WHERE g.game_id = p.game_id AND g.league = p.league AND p.league = :'league' :psp ;

UPDATE pbp.scrimmage_play p
   SET venue_id = g.venue_id, neutral_site = g.neutral_site,
       conference_game = g.conference_game
  FROM pbp.fact_game g
 WHERE g.game_id = p.game_id AND g.league = p.league AND p.league = :'league' :psp ;

COMMIT;

ANALYZE pbp.special_teams_play;
ANALYZE pbp.scrimmage_play;
ANALYZE pbp.scrimmage_athlete;
ANALYZE pbp.drive;
ANALYZE pbp.fact_game;
ANALYZE pbp.dim_team_season;

\echo '=== row counts by league'
SELECT 'special_teams_play' AS t, league, count(*) FROM pbp.special_teams_play GROUP BY 1,2
UNION ALL SELECT 'scrimmage_play', league, count(*) FROM pbp.scrimmage_play GROUP BY 1,2
UNION ALL SELECT 'drive', league, count(*) FROM pbp.drive GROUP BY 1,2
UNION ALL SELECT 'fact_game', league, count(*) FROM pbp.fact_game GROUP BY 1,2
UNION ALL SELECT 'dim_team', league, count(*) FROM pbp.dim_team GROUP BY 1,2
UNION ALL SELECT 'dim_team_season', league, count(*) FROM pbp.dim_team_season GROUP BY 1,2
UNION ALL SELECT 'dim_conference', league, count(*) FROM pbp.dim_conference GROUP BY 1,2
UNION ALL SELECT 'dim_venue', '(shared)', count(*) FROM pbp.dim_venue
ORDER BY 1,2;
