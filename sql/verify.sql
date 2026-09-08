\echo '=== row counts by season and kind'
SELECT season,
       count(*) FILTER (WHERE play_kind='kickoff')    AS kickoff,
       count(*) FILTER (WHERE play_kind='punt')       AS punt,
       count(*) FILTER (WHERE play_kind='field_goal') AS field_goal,
       count(*) FILTER (WHERE play_kind='pat')        AS pat,
       count(*) FILTER (WHERE play_kind='two_point')  AS two_pt,
       count(*) AS total
FROM st.special_teams_play GROUP BY season ORDER BY season;

\echo '=== source split + wallclock coverage'
SELECT source, min(season) AS from_season, max(season) AS to_season, count(*) AS rows,
       count(wallclock_utc) AS with_wallclock
FROM st.special_teams_play GROUP BY source;

\echo '=== field goal accuracy by distance (must decline monotonically)'
SELECT width_bucket(fg_distance_yds, 15, 60, 9) * 5 + 10 AS dist_bucket_start,
       count(*) AS att, round(100.0*count(*) FILTER (WHERE fg_made)/count(*), 1) AS pct_made
FROM st.special_teams_play
WHERE play_kind='field_goal' AND fg_made IS NOT NULL AND fg_distance_yds BETWEEN 15 AND 59
GROUP BY 1 ORDER BY 1;

\echo '=== punt gross average by season (FBS reality ~41-43)'
SELECT season, count(*) AS punts, round(avg(punt_gross_yds)::numeric, 2) AS avg_gross
FROM st.special_teams_play
WHERE play_kind='punt' AND NOT kick_blocked AND punt_gross_yds > 0
GROUP BY season ORDER BY season;

\echo '=== kickoff touchback rate by season (2018 fair-catch rule should show)'
SELECT season, count(*) AS kickoffs,
       round(100.0*count(*) FILTER (WHERE touchback)/count(*), 1) AS tb_pct
FROM st.special_teams_play
WHERE play_kind='kickoff' AND NOT onside AND kickoff_yds IS NOT NULL
GROUP BY season ORDER BY season;

\echo '=== PAT conversion by season (FBS reality ~96-97)'
SELECT season, count(*) AS pats,
       round(100.0*count(*) FILTER (WHERE converted)/count(*), 2) AS pct_good
FROM st.special_teams_play WHERE play_kind='pat' GROUP BY season ORDER BY season;

\echo '=== parse confidence distribution'
SELECT play_kind, parse_confidence, count(*)
FROM st.special_teams_play GROUP BY 1,2 ORDER BY 1,2;

\echo '=== integrity: duplicate uids, orphan kinds, impossible values'
SELECT 'impossible FG distance' AS check, count(*) FROM st.special_teams_play
  WHERE fg_distance_yds IS NOT NULL AND (fg_distance_yds < 15 OR fg_distance_yds > 70)
UNION ALL SELECT 'punt gross > 90', count(*) FROM st.special_teams_play WHERE punt_gross_yds > 90
UNION ALL SELECT 'kickoff yds > 100', count(*) FROM st.special_teams_play WHERE kickoff_yds > 100
UNION ALL SELECT 'null kicking_team', count(*) FROM st.special_teams_play WHERE kicking_team_id IS NULL
UNION ALL SELECT 'kicking = receiving', count(*) FROM st.special_teams_play
  WHERE kicking_team_id = receiving_team_id;
