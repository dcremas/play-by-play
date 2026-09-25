-- Phase 4 dimensions.
CREATE SCHEMA IF NOT EXISTS pbp;

DROP TABLE IF EXISTS pbp.dim_conference CASCADE;
CREATE TABLE pbp.dim_conference (
  conference_id   integer PRIMARY KEY,
  conference_name text NOT NULL,
  short_name      text
);

-- Slowly changing on purpose. Between 2014 and 2026 the Pac-12 went 12 teams -> 2 -> 8, the
-- Big Ten 14 -> 18, the Big 12 10 -> 16; 83 of 275 teams changed conference at least once.
-- Never denormalise conference onto a team-only dimension.
DROP TABLE IF EXISTS pbp.dim_team_season CASCADE;
CREATE TABLE pbp.dim_team_season (
  team_id         integer NOT NULL,
  season          smallint NOT NULL,
  conference_id   integer,
  conference_name text,
  ncaa_division   text,          -- FBS | FCS. College only; see nfl_division
  PRIMARY KEY (team_id, season)
);

DROP TABLE IF EXISTS pbp.dim_team CASCADE;
CREATE TABLE pbp.dim_team (
  team_id      integer PRIMARY KEY,
  display_name text
);

DROP TABLE IF EXISTS pbp.dim_venue CASCADE;
CREATE TABLE pbp.dim_venue (
  venue_id   integer PRIMARY KEY,
  venue_name text,
  city       text,
  state      text,
  zip        text,
  country    text,
  surface    text,              -- grass | turf
  indoor     boolean            -- from the SCOREBOARD payload, not the summary
);

DROP TABLE IF EXISTS pbp.fact_game CASCADE;
CREATE TABLE pbp.fact_game (
  game_id         bigint PRIMARY KEY,
  season          smallint NOT NULL,
  week            smallint,
  season_type     text,
  kickoff_utc     timestamptz,
  home_team_id    integer,
  away_team_id    integer,
  venue_id        integer REFERENCES pbp.dim_venue(venue_id),
  attendance      integer,
  neutral_site    boolean,
  conference_game boolean
);
CREATE INDEX ON pbp.fact_game (season, week);
CREATE INDEX ON pbp.fact_game (venue_id);

COMMENT ON COLUMN pbp.dim_venue.indoor IS
  'Roof, as ESPN states it -- and ONLY the scoreboard payload states it. summary.gameInfo.venue
   carries `grass` but never `indoor`, so a venue loader that reads only the summary (as this
   one did until 2026-09-09) can record surface and not roof.

   A STADIUM PROPERTY, NOT A GAME CONDITION. A retractable roof reads true whether or not it
   was open that day, and 5 of the 18 indoor venues are retractable. Do not read indoor = true
   as "weather does not apply here"; it means "weather may not apply, and the feed cannot say".
   Consistent across all 10,470 games -- no venue reports both values -- so the venue grain is
   the feed''s own grain, not an aggregation this project chose.';

COMMENT ON TABLE pbp.dim_team_season IS
  'Conference membership BY SEASON. Joining a play to a conference must go through
   (team_id, season) -- a team-only join silently backdates 2024 realignment across all
   ten seasons.';
