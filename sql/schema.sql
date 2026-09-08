-- D-I FBS special teams fact table.
-- One row per placekick, kickoff, or punt, 2014 onward.
-- Source: ESPN play-by-play, fetched directly for every season.

CREATE SCHEMA IF NOT EXISTS pbp;

DROP TABLE IF EXISTS pbp.special_teams_play;
CREATE TABLE pbp.special_teams_play (
  play_uid            text PRIMARY KEY,
  source              text NOT NULL,          -- 'espn'
  game_id             bigint NOT NULL,
  season              smallint NOT NULL,
  week                smallint,
  season_type         text,                   -- regular | postseason
  play_kind           text NOT NULL,          -- kickoff | punt | field_goal | pat | two_point

  -- situation
  period              smallint,
  clock_secs_period   integer,                -- seconds remaining in the period
  wallclock_utc       timestamptz,            -- per-play; enables play-level weather joins
  down                smallint,
  distance            smallint,
  yards_to_goal       smallint,
  kicking_team_id     integer,                -- ESPN start.team.id: ALWAYS the kicking team
  receiving_team_id   integer,
  is_home_kicking     boolean,
  score_diff_kicking  smallint,               -- kicking team's margin before the play

  -- outcome (nullable by kind)
  fg_distance_yds     smallint,
  fg_made             boolean,                -- NULL when the kick was wiped out by penalty
  punt_gross_yds      smallint,
  punt_net_yds        smallint,
  kickoff_yds         smallint,
  return_yds          smallint,               -- 0 = not advanced; see `returned`
  returned            boolean,                -- was a return actually attempted
  touchback           boolean,
  onside              boolean,
  fair_catch          boolean,
  downed              boolean,
  out_of_bounds       boolean,
  kick_blocked        boolean,
  returned_for_td     boolean,
  converted           boolean,                -- pat / two_point only
  two_point_type      text,                   -- pass | rush
  miss_reason         text,                   -- wide right | wide left | short | ...
  negated_by_penalty  boolean,

  -- people (athlete ids are Phase 4; names are what the feed gives)
  kicker_name         text,
  returner_name       text,
  blocker_name        text,


  -- provenance
  play_text           text,
  parse_confidence    text,                   -- exact | partial | ambiguous
  loaded_at           timestamptz DEFAULT now()
);

CREATE INDEX ON pbp.special_teams_play (season, play_kind);
CREATE INDEX ON pbp.special_teams_play (kicking_team_id, season);
CREATE INDEX ON pbp.special_teams_play (play_kind, fg_distance_yds) WHERE play_kind = 'field_goal';
CREATE INDEX ON pbp.special_teams_play (wallclock_utc);
CREATE INDEX ON pbp.special_teams_play (game_id);

COMMENT ON COLUMN pbp.special_teams_play.return_yds IS
  'Actual yards returned. 0 means the ball was not advanced (touchback, fair catch, downed,
   out of bounds) -- check `returned` to exclude those from return averages.';
COMMENT ON COLUMN pbp.special_teams_play.score_diff_kicking IS
  'The kicking team''s margin BEFORE the play, from the repaired running score. On a
   conversion row -- which is derived from the touchdown play -- it is the margin the KICKER
   faced: after the touchdown, before his own kick. Until 2026-09-08 this was computed from
   ESPN''s homeScore/awayScore, which are the score AFTER the play, so a made field goal
   carried a margin that already included the three points it had just scored.';
COMMENT ON COLUMN pbp.special_teams_play.kicking_team_id IS
  'ESPN start.team.id. Verified over 105k plays: this is the kicking team for every kind --
   the offense on punts/FGs, the defense on kickoffs.';
