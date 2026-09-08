-- Stage-2 load, step 2 of 2: cast staging into the typed tables and drop staging.
TRUNCATE st.scrimmage_athlete;
INSERT INTO st.scrimmage_athlete (play_uid, role, athlete_id, ordinal)
SELECT play_uid, role,
       NULLIF(athlete_id,'')::numeric::bigint,
       NULLIF(ordinal,'')::numeric::smallint
FROM st.stg_scrimmage_athlete;

TRUNCATE st.drive;
INSERT INTO st.drive (
  drive_uid, drive_id, game_id, season, week, season_type, drive_number,
  offense_team_id, defense_team_id, result, display_result, description, is_score,
  offensive_plays, plays_total, plays_scrimmage, yards, time_elapsed_secs,
  start_period, start_clock_secs, start_yards_to_goal, start_text,
  end_period, end_clock_secs, end_yards_to_goal, end_text)
SELECT
  drive_uid, NULLIF(drive_id,''),
  NULLIF(game_id,'')::numeric::bigint,
  NULLIF(season,'')::numeric::smallint,
  NULLIF(week,'')::numeric::smallint,
  NULLIF(season_type,''),
  NULLIF(drive_number,'')::numeric::smallint,
  NULLIF(offense_team_id,'')::numeric::integer,
  NULLIF(defense_team_id,'')::numeric::integer,
  NULLIF(result,''), NULLIF(display_result,''), NULLIF(description,''),
  NULLIF(is_score,'')::boolean,
  NULLIF(offensive_plays,'')::numeric::smallint,
  NULLIF(plays_total,'')::numeric::smallint,
  NULLIF(plays_scrimmage,'')::numeric::smallint,
  NULLIF(yards,'')::numeric::smallint,
  NULLIF(time_elapsed_secs,'')::numeric::integer,
  NULLIF(start_period,'')::numeric::smallint,
  NULLIF(start_clock_secs,'')::numeric::integer,
  NULLIF(start_yards_to_goal,'')::numeric::smallint,
  NULLIF(start_text,''),
  NULLIF(end_period,'')::numeric::smallint,
  NULLIF(end_clock_secs,'')::numeric::integer,
  NULLIF(end_yards_to_goal,'')::numeric::smallint,
  NULLIF(end_text,'')
FROM st.stg_drive;

DROP TABLE st.stg_scrimmage_athlete;
DROP TABLE st.stg_drive;
ANALYZE st.scrimmage_athlete;
ANALYZE st.drive;
