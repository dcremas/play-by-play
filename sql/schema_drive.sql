-- D-I FBS drive table.
-- One row per drive, 2014 onward. 258,795 rows over 10,301 games -- 23.8 drives a game.
--
-- Deliberately NOT scrimmage-only. A drive that ends in a punt contains the punt, so this
-- table spans both facts and plays_total counts every play in the drive while
-- plays_scrimmage counts only those in pbp.scrimmage_play.

CREATE SCHEMA IF NOT EXISTS pbp;

DROP TABLE IF EXISTS pbp.drive;
CREATE TABLE pbp.drive (
  drive_uid           text PRIMARY KEY,   -- 'espn:<game_id>:d<drive_number>'
  drive_id            text,               -- ESPN's own id; scrimmage_play.drive_id joins here
  game_id             bigint NOT NULL,
  season              smallint NOT NULL,
  week                smallint,
  season_type         text,
  drive_number        smallint NOT NULL,  -- 1-based within the game

  offense_team_id     integer,
  defense_team_id     integer,

  result              text,               -- PUNT | TD | FG | INT | DOWNS | FUMBLE | ...
  display_result      text,
  description         text,               -- '12 plays, 75 yards, 6:16'
  is_score            boolean,

  offensive_plays     smallint,           -- ESPN's own count
  plays_total         smallint,           -- every play in the drive, kicks included
  plays_scrimmage     smallint,           -- only those that reached pbp.scrimmage_play
  yards               smallint,
  time_elapsed_secs   integer,

  start_period        smallint,
  start_clock_secs    integer,
  start_yards_to_goal smallint,
  start_text          text,               -- 'UNM 25'
  end_period          smallint,
  end_clock_secs      integer,
  end_yards_to_goal   smallint,
  end_text            text
);

CREATE INDEX ON pbp.drive (game_id);
CREATE INDEX ON pbp.drive (season, result);
CREATE INDEX ON pbp.drive (offense_team_id, season);
CREATE INDEX ON pbp.drive (drive_id);

COMMENT ON COLUMN pbp.drive.start_yards_to_goal IS
  'Taken from the FIRST play''s start.yardsToEndzone, not from drive.start.yardLine. That
   ESPN column is measured in a fixed direction rather than from the possessing team''s own
   goal, so it reads 25 for one team''s own 25 and 76 for the other''s own 24. The play-level
   column is offense-relative and 100% populated.';
COMMENT ON COLUMN pbp.drive.drive_id IS
  'ESPN''s id. drive_uid is the primary key because this one is the feed''s, not ours.';
