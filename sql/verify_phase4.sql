\echo '=== athlete link coverage by season'
SELECT season,
       count(*) AS st_plays,
       round(100.0*count(kicker_athlete_id)/count(*),1)   AS pct_kicker_id,
       round(100.0*count(returner_athlete_id)/count(*),1) AS pct_returner_id
FROM pbp.special_teams_play
WHERE play_kind IN ('punt','kickoff','field_goal')
GROUP BY season ORDER BY season;

\echo '=== does the athlete id beat the name? name collisions across distinct players'
SELECT count(*) AS names_shared_by_multiple_athletes FROM (
  SELECT known_name FROM pbp.dim_athlete
  WHERE known_name IS NOT NULL GROUP BY known_name HAVING count(*) > 1) x;

\echo '=== busiest placekickers by attempts, resolved by athlete id'
SELECT a.known_name, t.display_name AS team, a.first_season, a.last_season,
       count(*) AS fg_att,
       round(100.0*count(*) FILTER (WHERE p.fg_made)/count(*),1) AS pct
FROM pbp.special_teams_play p
JOIN pbp.dim_athlete a ON a.athlete_id = p.kicker_athlete_id
LEFT JOIN pbp.dim_team t ON t.team_id = a.primary_team_id
WHERE p.play_kind='field_goal' AND p.fg_made IS NOT NULL
GROUP BY 1,2,3,4 ORDER BY fg_att DESC LIMIT 8;

\echo '=== THE CONFERENCE TRAP: same query, team-only join vs team-season join'
\echo '--- WRONG: conference taken from the teams latest season (backdates realignment)'
WITH latest AS (
  SELECT DISTINCT ON (team_id) team_id, conference_name
  FROM pbp.dim_team_season ORDER BY team_id, season DESC)
SELECT l.conference_name, count(*) AS punts
FROM pbp.special_teams_play p JOIN latest l ON l.team_id = p.kicking_team_id
WHERE p.play_kind='punt' AND p.season = 2018
  AND l.conference_name IN ('Big Ten Conference','Pac-12 Conference')
GROUP BY 1 ORDER BY 1;

\echo '--- RIGHT: conference as of that season'
SELECT ts.conference_name, count(*) AS punts
FROM pbp.special_teams_play p
JOIN pbp.dim_team_season ts ON ts.team_id = p.kicking_team_id AND ts.season = p.season
WHERE p.play_kind='punt' AND p.season = 2018
  AND ts.conference_name IN ('Big Ten Conference','Pac-12 Conference')
GROUP BY 1 ORDER BY 1;

\echo '=== field goal accuracy by surface (venue dimension)'
SELECT v.surface, count(*) AS att,
       round(100.0*count(*) FILTER (WHERE p.fg_made)/count(*),1) AS pct_made,
       round(avg(p.fg_distance_yds),1) AS avg_dist
FROM pbp.special_teams_play p JOIN pbp.dim_venue v ON v.venue_id = p.venue_id
WHERE p.play_kind='field_goal' AND p.fg_made IS NOT NULL
GROUP BY 1 ORDER BY 1;

\echo '=== referential integrity'
SELECT 'plays with no game row' AS check, count(*) FROM pbp.special_teams_play p
  LEFT JOIN pbp.fact_game g ON g.game_id=p.game_id WHERE g.game_id IS NULL
UNION ALL SELECT 'kicker id not in dim_athlete', count(*) FROM pbp.special_teams_play p
  LEFT JOIN pbp.dim_athlete a ON a.athlete_id=p.kicker_athlete_id
  WHERE p.kicker_athlete_id IS NOT NULL AND a.athlete_id IS NULL
UNION ALL SELECT 'kicking team missing team-season', count(*) FROM pbp.special_teams_play p
  LEFT JOIN pbp.dim_team_season ts ON ts.team_id=p.kicking_team_id AND ts.season=p.season
  WHERE p.kicking_team_id IS NOT NULL AND ts.team_id IS NULL
UNION ALL SELECT 'venue_id not in dim_venue', count(*) FROM pbp.special_teams_play p
  LEFT JOIN pbp.dim_venue v ON v.venue_id=p.venue_id
  WHERE p.venue_id IS NOT NULL AND v.venue_id IS NULL;
