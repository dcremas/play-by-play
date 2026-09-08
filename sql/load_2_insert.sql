-- Step 2 of 2. Casts staging text into the typed fact table and drops staging.
TRUNCATE st.special_teams_play;
INSERT INTO st.special_teams_play (
  play_uid, source, game_id, season, week, season_type, play_kind, period,
  clock_secs_period, wallclock_utc, down, distance, yards_to_goal, kicking_team_id,
  receiving_team_id, is_home_kicking, score_diff_kicking, fg_distance_yds, fg_made,
  punt_gross_yds, punt_net_yds, kickoff_yds, return_yds, returned, touchback, onside,
  fair_catch, downed, out_of_bounds, kick_blocked, returned_for_td, converted,
  two_point_type, miss_reason, negated_by_penalty, kicker_name, returner_name,
  blocker_name, play_text, parse_confidence)
SELECT
  play_uid, source,
  NULLIF(game_id,'')::numeric::bigint,
  NULLIF(season,'')::numeric::smallint,
  NULLIF(week,'')::numeric::smallint,
  NULLIF(season_type,''), NULLIF(play_kind,''),
  NULLIF(period,'')::numeric::smallint,
  NULLIF(clock_secs_period,'')::numeric::integer,
  NULLIF(wallclock_utc,'')::timestamptz,
  NULLIF(down,'')::numeric::smallint,
  NULLIF(distance,'')::numeric::smallint,
  NULLIF(yards_to_goal,'')::numeric::smallint,
  NULLIF(kicking_team_id,'')::numeric::integer,
  NULLIF(receiving_team_id,'')::numeric::integer,
  NULLIF(is_home_kicking,'')::boolean,
  NULLIF(score_diff_kicking,'')::numeric::smallint,
  NULLIF(fg_distance_yds,'')::numeric::smallint,
  NULLIF(fg_made,'')::boolean,
  NULLIF(punt_gross_yds,'')::numeric::smallint,
  NULLIF(punt_net_yds,'')::numeric::smallint,
  NULLIF(kickoff_yds,'')::numeric::smallint,
  NULLIF(return_yds,'')::numeric::smallint,
  NULLIF(returned,'')::boolean,
  NULLIF(touchback,'')::boolean,
  NULLIF(onside,'')::boolean,
  NULLIF(fair_catch,'')::boolean,
  NULLIF(downed,'')::boolean,
  NULLIF(out_of_bounds,'')::boolean,
  NULLIF(kick_blocked,'')::boolean,
  NULLIF(returned_for_td,'')::boolean,
  NULLIF(converted,'')::boolean,
  NULLIF(two_point_type,''), NULLIF(miss_reason,''),
  NULLIF(negated_by_penalty,'')::boolean,
  NULLIF(kicker_name,''), NULLIF(returner_name,''), NULLIF(blocker_name,''),
  NULLIF(play_text,''), NULLIF(parse_confidence,'')
FROM st.stg_plays;

DROP TABLE st.stg_plays;
ANALYZE st.special_teams_play;
