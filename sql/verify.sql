\echo '=== row counts by season and kind'
SELECT season,
       count(*) FILTER (WHERE play_kind='kickoff')    AS kickoff,
       count(*) FILTER (WHERE play_kind='punt')       AS punt,
       count(*) FILTER (WHERE play_kind='field_goal') AS field_goal,
       count(*) FILTER (WHERE play_kind='pat')        AS pat,
       count(*) FILTER (WHERE play_kind='two_point')  AS two_pt,
       count(*) AS total
FROM pbp.special_teams_play GROUP BY season ORDER BY season;

\echo '=== source split + wallclock coverage'
SELECT source, min(season) AS from_season, max(season) AS to_season, count(*) AS rows,
       count(wallclock_utc) AS with_wallclock
FROM pbp.special_teams_play GROUP BY source;

\echo '=== field goal accuracy by distance (must decline monotonically)'
SELECT width_bucket(fg_distance_yds, 15, 60, 9) * 5 + 10 AS dist_bucket_start,
       count(*) AS att, round(100.0*count(*) FILTER (WHERE fg_made)/count(*), 1) AS pct_made
FROM pbp.special_teams_play
WHERE play_kind='field_goal' AND fg_made IS NOT NULL AND fg_distance_yds BETWEEN 15 AND 59
GROUP BY 1 ORDER BY 1;

\echo '=== punt gross average by season (FBS reality ~41-43)'
SELECT season, count(*) AS punts, round(avg(punt_gross_yds)::numeric, 2) AS avg_gross
FROM pbp.special_teams_play
WHERE play_kind='punt' AND NOT kick_blocked AND punt_gross_yds > 0
GROUP BY season ORDER BY season;

\echo '=== kickoff touchback rate by season (2018 fair-catch rule should show)'
SELECT season, count(*) AS kickoffs,
       round(100.0*count(*) FILTER (WHERE touchback)/count(*), 1) AS tb_pct
FROM pbp.special_teams_play
WHERE play_kind='kickoff' AND NOT onside AND kickoff_yds IS NOT NULL
GROUP BY season ORDER BY season;

\echo '=== PAT conversion by season (FBS reality ~96-97)'
SELECT season, count(*) AS pats,
       round(100.0*count(*) FILTER (WHERE converted)/count(*), 2) AS pct_good
FROM pbp.special_teams_play WHERE play_kind='pat' GROUP BY season ORDER BY season;

\echo '=== parse confidence distribution'
SELECT play_kind, parse_confidence, count(*)
FROM pbp.special_teams_play GROUP BY 1,2 ORDER BY 1,2;

\echo '=== integrity: duplicate uids, orphan kinds, impossible values'
SELECT 'impossible FG distance' AS check, count(*) FROM pbp.special_teams_play
  WHERE fg_distance_yds IS NOT NULL AND (fg_distance_yds < 15 OR fg_distance_yds > 70)
UNION ALL SELECT 'punt gross > 90', count(*) FROM pbp.special_teams_play WHERE punt_gross_yds > 90
UNION ALL SELECT 'kickoff yds > 100', count(*) FROM pbp.special_teams_play WHERE kickoff_yds > 100
UNION ALL SELECT 'null kicking_team', count(*) FROM pbp.special_teams_play WHERE kicking_team_id IS NULL
UNION ALL SELECT 'kicking = receiving', count(*) FROM pbp.special_teams_play
  WHERE kicking_team_id = receiving_team_id;

-- ============================================================================================
-- CROSS-LEAGUE INVARIANTS
--
-- Added 2026-09-11 with the NFL. Every one of these was true when the NFL was first loaded;
-- they are here because "was true once" and "is a property of the schema" are different
-- claims, and only the second one survives a reload. Each must return ZERO.
-- ============================================================================================

\echo '=== [must be 0] play_uid duplicated across the two facts or the two leagues'
SELECT count(*) AS duplicate_play_uids FROM (
  SELECT play_uid FROM pbp.special_teams_play
  UNION ALL SELECT play_uid FROM pbp.scrimmage_play
) u GROUP BY play_uid HAVING count(*) > 1;

\echo '=== [must be 0] a game_id claimed by both leagues'
-- The ranges interleave (college 400547640-401870790, NFL 400554214-401772954), so
-- non-collision is a fact about ESPN's id allocation and not a structural guarantee. Test
-- it; do not trust it.
SELECT count(*) AS cross_league_game_ids FROM (
  SELECT game_id FROM pbp.fact_game GROUP BY game_id HAVING count(DISTINCT league) > 1
) x;

\echo '=== [must be 0] a fact row whose game is missing from fact_game IN ITS OWN LEAGUE'
SELECT 'special_teams_play' AS fact, count(*) AS orphan_plays
FROM pbp.special_teams_play p
WHERE NOT EXISTS (SELECT 1 FROM pbp.fact_game g
                   WHERE g.game_id = p.game_id AND g.league = p.league)
UNION ALL
SELECT 'scrimmage_play', count(*) FROM pbp.scrimmage_play p
WHERE NOT EXISTS (SELECT 1 FROM pbp.fact_game g
                   WHERE g.game_id = p.game_id AND g.league = p.league);

\echo '=== [must be 0] a team on a play that its own league''s dimension does not know'
-- The point of keying dim_team by (league, team_id): team 2 is Auburn in one league and
-- nobody in the other, so a league-blind join would silently succeed with the wrong team.
SELECT p.league, count(DISTINCT p.offense_team_id) AS unknown_teams
FROM pbp.scrimmage_play p
WHERE p.offense_team_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM pbp.dim_team t
                   WHERE t.league = p.league AND t.team_id = p.offense_team_id)
GROUP BY 1;

\echo '=== [must be 0] Pro Bowl all-star squads anywhere in the NFL corpus'
-- ESPN gives the AFC and NFC all-star teams ids 31 and 32 and files the game as postseason
-- week 4. They are rosters that do not exist; fetch_espn drops the 8 games at fetch time.
SELECT count(*) AS probowl_rows FROM pbp.scrimmage_play
WHERE league = 'nfl' AND (offense_team_id IN (31,32) OR defense_team_id IN (31,32));

\echo '=== [must be 0] division/nfl_division used by the wrong league'
-- `division` means FBS|FCS and is college-only; `nfl_division` means AFC East and is
-- NFL-only. Either column populated for the other league means the two meanings have
-- started to merge.
SELECT count(*) AS mislabelled FROM pbp.dim_team_season
WHERE (league = 'nfl' AND ncaa_division IS NOT NULL)
   OR (league = 'cfb' AND nfl_division IS NOT NULL);

\echo '=== athletes by corpus -- cfb+nfl is ONE person who played in both'
SELECT leagues, count(*) AS athletes FROM pbp.dim_athlete GROUP BY 1 ORDER BY 2 DESC;
