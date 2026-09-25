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

-- Roof coverage and behaviour. Two things this catches. If `indoor` goes all-NULL the
-- games lists were rebuilt by a fetcher that does not capture `venue_indoor` -- only the
-- SCOREBOARD states a roof, and a stage_games without it discards the field silently.
-- If the indoor/outdoor ordering inverts or collapses, the flag is joined to the wrong
-- thing: a roof raises all three of these measures, and did by 2.4 points of FG%, 0.97
-- yards of punt gross and 1.7 points of touchback rate when it was built on 2026-09-09.
\echo '=== roof coverage (NULL here on anything but the 14 venue-less games means the fetcher dropped venue_indoor)'
SELECT count(*) AS venues,
       count(*) FILTER (WHERE indoor)          AS indoor,
       count(*) FILTER (WHERE indoor IS FALSE) AS outdoor,
       count(*) FILTER (WHERE indoor IS NULL)  AS unknown
FROM pbp.dim_venue;

\echo '=== kicking under a roof vs in the open (FBS v FBS; a roof should raise all three)'
SELECT CASE WHEN v.indoor THEN 'indoor' ELSE 'outdoor' END AS roof,
       count(*) FILTER (WHERE p.play_kind='field_goal' AND p.fg_made IS NOT NULL) AS fg_att,
       round(100.0*avg(CASE WHEN p.play_kind='field_goal' AND p.fg_made IS NOT NULL
                            THEN p.fg_made::int END),2) AS fg_pct,
       round(avg(CASE WHEN p.play_kind='punt' THEN p.punt_gross_yds END),2) AS punt_gross,
       round(100.0*avg(CASE WHEN p.play_kind='kickoff' AND p.touchback IS NOT NULL
                            THEN p.touchback::int END),2) AS tb_pct
FROM pbp.special_teams_play p
JOIN pbp.dim_venue v ON v.venue_id = p.venue_id
JOIN pbp.dim_team_season kts ON kts.team_id = p.kicking_team_id   AND kts.season = p.season
                            AND kts.league = p.league
JOIN pbp.dim_team_season rts ON rts.team_id = p.receiving_team_id AND rts.season = p.season
                            AND rts.league = p.league
WHERE v.indoor IS NOT NULL AND kts.ncaa_division='FBS' AND rts.ncaa_division='FBS'
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
