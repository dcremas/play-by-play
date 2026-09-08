-- D-I FBS scrimmage play fact table.
-- One row per rush, pass, sack, penalty or unresolved scrimmage play, 2014 onward.
-- Source: ESPN play-by-play, the same summaries that feed pbp.special_teams_play.
--
-- Sibling of pbp.special_teams_play, not a replacement. The two are disjoint by construction:
-- build_scrimmage.py excludes whatever build_table.classify() claims, calling that function
-- rather than re-listing its rules. Together they cover every non-administrative play in the
-- corpus, and play_uid is unique across BOTH tables.
--
-- Unlike the kicks, almost nothing here is parsed. statYardage, down, distance, isTurnover
-- and scoringPlay are structured fields at 100% coverage; play text is provenance, not input.

CREATE SCHEMA IF NOT EXISTS pbp;

DROP TABLE IF EXISTS pbp.scrimmage_play;
CREATE TABLE pbp.scrimmage_play (
  play_uid            text PRIMARY KEY,
  source              text NOT NULL,          -- 'espn'
  game_id             bigint NOT NULL,
  season              smallint NOT NULL,
  week                smallint,
  season_type         text,                   -- regular | postseason
  play_kind           text NOT NULL,          -- rush | pass | sack | penalty | other
  play_type_espn      text,                   -- ESPN's raw label; play_kind is derived from it

  -- drive
  drive_id            text,
  drive_number        smallint,               -- 1-based within the game

  -- situation
  period              smallint,
  clock_secs_period   integer,                -- seconds remaining in the period
  wallclock_utc       timestamptz,            -- per-play; enables play-level weather joins
  down                smallint,
  distance            smallint,
  yards_to_goal       smallint,
  offense_team_id     integer,
  defense_team_id     integer,
  is_home_offense     boolean,
  score_diff_offense  smallint,               -- offense's margin BEFORE the snap

  -- outcome, read off structured fields
  yards_gained        smallint,               -- statYardage
  end_down            smallint,
  end_distance        smallint,
  end_yards_to_goal   smallint,
  end_team_id         integer,                -- who holds the ball AFTER the play
  first_down_gained   boolean,
  is_complete         boolean,                -- pass plays only
  is_touchdown        boolean,
  is_turnover         boolean,
  is_penalty          boolean,
  is_scoring_play     boolean,
  points_scored       smallint,               -- signed, from the OFFENSE's perspective

  -- people (denormalised convenience; pbp.scrimmage_athlete is the full-fidelity bridge)
  passer_athlete_id   bigint,
  rusher_athlete_id   bigint,
  receiver_athlete_id bigint,
  tackler_athlete_id  bigint,

  -- provenance
  play_text           text,
  loaded_at           timestamptz DEFAULT now(),

  -- game context, filled by sql/enrich_game_context.sql AFTER the load
  venue_id            integer,
  neutral_site        boolean,
  conference_game     boolean
);

CREATE INDEX ON pbp.scrimmage_play (season, play_kind);
CREATE INDEX ON pbp.scrimmage_play (offense_team_id, season);
CREATE INDEX ON pbp.scrimmage_play (defense_team_id, season);
CREATE INDEX ON pbp.scrimmage_play (game_id);
CREATE INDEX ON pbp.scrimmage_play (wallclock_utc);
CREATE INDEX ON pbp.scrimmage_play (passer_athlete_id) WHERE passer_athlete_id IS NOT NULL;
CREATE INDEX ON pbp.scrimmage_play (rusher_athlete_id) WHERE rusher_athlete_id IS NOT NULL;
CREATE INDEX ON pbp.scrimmage_play (receiver_athlete_id) WHERE receiver_athlete_id IS NOT NULL;

COMMENT ON TABLE pbp.scrimmage_play IS
  'One row per scrimmage play, 2014 onward. Disjoint from pbp.special_teams_play by
   construction; play_uid is unique across both. Conversion attempts (PAT, two-point) live
   in pbp.special_teams_play, not here.';
COMMENT ON COLUMN pbp.scrimmage_play.play_kind IS
  'Derived, NOT a copy of play_type_espn. ESPN types a play by its most notable event, so a
   rush that ended in a fumble is typed "Fumble Recovery (Own)". Where the type names an
   outcome, the snap is recovered from the participant roles -- a passer role means a pass
   was thrown. That reclassifies ~33,000 plays into rush and pass. play_type_espn keeps the
   raw label so this is auditable.';
COMMENT ON COLUMN pbp.scrimmage_play.play_uid IS
  'espn:<game_id>:<sequenceNumber>. A ''#n'' suffix means ESPN reused one sequenceNumber for
   more than one play -- 464 rows. 131 of those collide with a kick in pbp.special_teams_play,
   which keeps the unsuffixed form. Participants are keyed on sequenceNumber, so both rows of
   a collision inherit the same athletes and at most one of them is right.';
COMMENT ON COLUMN pbp.scrimmage_play.end_yards_to_goal IS
  'Measured from the perspective of whoever holds the ball AFTER the play -- see end_team_id.
   On a turnover it flips to the other goal line, so it is NOT comparable with yards_to_goal
   unless end_team_id = offense_team_id.';
COMMENT ON COLUMN pbp.scrimmage_play.score_diff_offense IS
  'The margin BEFORE the snap, from the running score carried into the play. Note this
   differs from pbp.special_teams_play.score_diff_kicking, which documents the same intent but
   is computed from the after-play scoreboard columns.';
COMMENT ON COLUMN pbp.scrimmage_play.points_scored IS
  'Points the OFFENSE gained on this play, signed: a pick-six is negative. Includes the
   conversion when ESPN folds it into the touchdown play text, so 7 is the common value.';


-- ---------------------------------------------------------------------------------------
-- The people bridge. Same shape as pbp.play_athlete, and the same reason for existing: the
-- four id columns on pbp.scrimmage_play are a denormalised hot path that keeps only the FIRST
-- athlete in each of four roles, while a real play has many tacklers and twelve roles.
-- 3,135,126 rows against 1,510,679 plays.
DROP TABLE IF EXISTS pbp.scrimmage_athlete;
CREATE TABLE pbp.scrimmage_athlete (
  play_uid    text NOT NULL,
  role        text NOT NULL,      -- rusher | passer | receiver | tackler | assistedBy | ...
  athlete_id  bigint NOT NULL,
  ordinal     smallint,           -- position within the play's participant list
  PRIMARY KEY (play_uid, role, athlete_id)
);

CREATE INDEX ON pbp.scrimmage_athlete (athlete_id);
CREATE INDEX ON pbp.scrimmage_athlete (role);

COMMENT ON TABLE pbp.scrimmage_athlete IS
  'One row per (play, role, athlete) on a scrimmage play. ESPN emits a (role, athlete) pair twice on one
   play often enough to cost 161,676 duplicate rows -- the same receiver listed twice -- and
   the repeat is dropped at build time so the primary key holds; ordinal keeps the feed order of what survives.';
